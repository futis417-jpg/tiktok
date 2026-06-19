import os
import gc
import re
import time
import shutil
import random
import asyncio
import sqlite3
import logging
import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from telegram.error import BadRequest
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import stealth_async

# Configuración avanzada de logging con niveles detallados para monitoreo en vivo
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Constantes, directorios físicos del bot y archivo de control
TOKEN = os.getenv("TELEGRAM_TOKEN", "TU_TOKEN_AQUI")
COOKIES_DIR = "cookies"
BAD_COOKIES_DIR = "cookies_caducadas"
PROXIES_FILE = "proxies.txt"
DB_FILE = "smm_bot.db"

# Asegurar de que existen todas las carpetas críticas para evitar excepciones de E/S
os.makedirs(COOKIES_DIR, exist_ok=True)
os.makedirs(BAD_COOKIES_DIR, exist_ok=True)

# Lock de base de datos asíncrono para prevenir colisiones de lectura/escritura concurrente
db_lock = asyncio.Lock()

def init_db():
    """Inicializa la base de datos local SQLite con esquemas robustos y tablas de rendimiento."""
    try:
        conn = sqlite3.connect(DB_FILE, timeout=15.0)
        cursor = conn.cursor()
        
        # Tabla de seguimientos exitosos para prevenir duplicados
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS seguimientos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_url TEXT,
                cookie_file TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Tabla de estado de salud de las cookies
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cookies_estado (
                cookie_file TEXT PRIMARY KEY,
                estado TEXT,
                motivo TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Tabla de rendimiento de proxies para priorizar las más rápidas y estables
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS proxy_rendimiento (
                proxy TEXT PRIMARY KEY,
                exitos INTEGER DEFAULT 0,
                fallos INTEGER DEFAULT 0,
                latencia REAL DEFAULT 0.0
            )
        ''')
        
        # Crear índice de búsqueda rápida para evitar lentitud cuando la base de datos crezca
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_seguimientos_target ON seguimientos(target_url)')
        conn.commit()
        conn.close()
        logger.info("Base de datos SQLite inicializada y optimizada correctamente.")
    except Exception as e:
        logger.error(f"Error crítico al inicializar la base de datos: {e}")

init_db()

# Mapeo de diccionarios multilingües para detectar estados de seguimiento en cuentas de cualquier país
FOLLOW_KEYWORDS = [
    "seguir", "follow", "подписаться", "folgen", "suivre", "takip et", "ikuti", "ติดตาม", "siga", "s’abonner", "theo dõi", "متابعة"
]

ALREADY_FOLLOWING_KEYWORDS = [
    "siguiendo", "following", "вы подписаны", "folgst du", "abonné", "takip ediliyor", "mengikuti", "กำลังติดตาม", "seguindo", "đang theo dõi", "يتابع"
]

MESSAGE_KEYWORDS = [
    "mensaje", "message", "enviar mensaje", "сообщение", "nachricht", "direct", "pesan", "ข้อความ", "tin nhắn", "رسالة", "mesaj"
]

def parse_netscape_cookies(file_path):
    """
    Parsea archivos de cookies en formato Netscape (TXT) extraídos del bot checker de Telegram.
    Filtra comentarios, metadatos informativos y sanitiza las variables de sesión de TikTok.
    """
    cookies = []
    try:
        if not os.path.exists(file_path):
            return None
            
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                # Ignorar comentarios, líneas vacías y metadatos que inserta el checker
                if not line or line.startswith('#') or 'Simple Checker' in line or 'Username:' in line or 'Cookies:' in line:
                    continue
                
                parts = line.split('\t')
                # El estándar Netscape requiere entre 6 y 7 campos delimitados por tabulaciones
                if len(parts) < 6:
                    continue

                domain = parts[0]
                # Asegurar que solo capturamos cookies legítimas del ecosistema de TikTok
                if "tiktok.com" not in domain:
                    continue
                
                path = parts[2]
                secure = parts[3].upper() == "TRUE"
                
                try:
                    expires = int(float(parts[4]))
                except ValueError:
                    expires = -1  # Marcar como cookie de sesión persistente si falla la conversión

                name = parts[5]
                value = parts[6] if len(parts) > 6 else ""

                cookies.append({
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path,
                    "secure": secure,
                    "expires": expires,
                    "httpOnly": False
                })
        
        if not cookies:
            logger.warning(f"No se pudieron extraer cookies válidas de: {file_path}")
            return None
            
        return {"cookies": cookies, "origins": []}
    except Exception as e:
        logger.error(f"Fallo durante el procesamiento del archivo de cookies {file_path}: {e}")
        return None

def load_proxies():
    """Carga de forma dinámica y resiliente las proxies desde el archivo proxies.txt."""
    if not os.path.exists(PROXIES_FILE):
        with open(PROXIES_FILE, 'w') as f:
            f.write("# Introduce tus proxies (socks5://IP:PUERTO), uno por línea\n")
        return []
    
    proxies = []
    try:
        with open(PROXIES_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    # Corregir de forma automática si carecen de protocolo explícito
                    if not (line.startswith("socks5://") or line.startswith("http://") or line.startswith("https://")):
                        line = f"socks5://{line}"
                    proxies.append(line)
        return proxies
    except Exception as e:
        logger.error(f"Error al leer la lista de proxies: {e}")
        return []

def get_random_fingerprint():
    """Genera huellas digitales dinámicas emparejando agentes de usuario con pantallas lógicas reales."""
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0"
    ]
    viewports = [
        {"width": 1366, "height": 768},
        {"width": 1920, "height": 1080},
        {"width": 1440, "height": 900},
        {"width": 1536, "height": 864}
    ]
    return random.choice(user_agents), random.choice(viewports)

async def resolve_tiktok_url(url: str) -> str:
    """
    Resuelve de forma asíncrona redirecciones móviles de TikTok (como vm.tiktok.com / vt.tiktok.com)
    para obtener el enlace directo de perfil antes de lanzar instancias de Playwright.
    """
    clean_url = url.strip()
    if "vm.tiktok.com" in clean_url or "vt.tiktok.com" in clean_url or "v.tiktok.com" in clean_url:
        logger.info(f"Detectado enlace acortado/móvil. Resolviendo redirección para: {clean_url}")
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=12.0) as client:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                }
                response = await client.get(clean_url, headers=headers)
                resolved = str(response.url).split("?")[0].rstrip("/")
                logger.info(f"Redirección resuelta con éxito: {resolved}")
                return resolved
        except Exception as e:
            logger.error(f"No se pudo resolver el enlace móvil de TikTok: {e}. Se usará el original.")
    return clean_url.split("?")[0].rstrip("/")

async def perform_action(proxy, cookie_path, target_url, semaphore):
    """
    Ejecuta de manera segura la apertura de sesión, validación de estado,
    esquiva de sistemas anti-bot, localización de idiomas y seguimiento físico.
    """
    async with semaphore:
        async with async_playwright() as p:
            # Configuración super-optimizada de Chromium para entornos con RAM limitada
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-gpu',
                    '--disable-dev-shm-usage',
                    '--single-process',
                    '--no-first-run',
                    '--disable-extensions'
                ]
            )
            
            cookie_data = parse_netscape_cookies(cookie_path)
            if not cookie_data:
                await browser.close()
                return False, "Estructura de cookies inválida o vacía.", None, False

            proxy_config = {"server": proxy} if proxy else None
            user_agent, viewport = get_random_fingerprint()
            
            try:
                # Forzamos la configuración regional e idioma en inglés para unificar la interfaz de TikTok
                context = await browser.new_context(
                    proxy=proxy_config,
                    storage_state=cookie_data,
                    viewport=viewport,
                    user_agent=user_agent,
                    locale="en-US",
                    timezone_id="America/New_York",
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"}
                )
                
                page = await context.new_page()
                await stealth_async(page)
                page.set_default_timeout(20000)
                
                filename_base = os.path.basename(cookie_path)
                logger.info(f"Abriendo perfil destino con la cuenta localizada: {filename_base}")
                
                await page.goto(target_url, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(2.5, 4.0))
                
                # --- SISTEMA DE VERIFICACIÓN DE SESIÓN ---
                login_btn = await page.query_selector('button[data-e2e="top-login-button"]')
                if login_btn:
                    screenshot_file = f"expirada_{int(time.time())}.png"
                    await page.screenshot(path=screenshot_file, full_page=True)
                    await browser.close()
                    return False, "La sesión ha caducado (Requiere Login)", screenshot_file, True

                # --- CONTROL DE CAPTCHAS EN PANTALLA ---
                captcha_detected = False
                captcha_selectors = ['#captcha-container', '.captcha_verify_container', 'div[class*="captcha"]', 'iframe[src*="captcha"]']
                for sel in captcha_selectors:
                    if await page.query_selector(sel):
                        captcha_detected = True
                        break
                        
                if captcha_detected:
                    screenshot_file = f"captcha_{int(time.time())}.png"
                    await page.screenshot(path=screenshot_file, full_page=True)
                    await browser.close()
                    return False, "Bloqueado temporalmente por CAPTCHA de TikTok.", screenshot_file, False

                # --- ANÁLISIS MULTILINGÜE DE RELACIÓN EXISTENTE ---
                page_content_lower = (await page.content()).lower()
                already_following = False

                # 1. Analizar si existe un botón de mensajería privada (prueba definitiva de seguimiento mutuo/existente)
                for key_msg in MESSAGE_KEYWORDS:
                    if f'has-text("{key_msg}")' in page_content_lower or key_msg in page_content_lower:
                        # Buscar si el botón de mensaje existe en la interfaz visible
                        msg_btn = await page.query_selector('button:has-text("{}")'.format(key_msg.capitalize()))
                        if msg_btn:
                            already_following = True
                            break

                # 2. Comprobar si el estado actual es de suscripción activa usando keywords internacionales
                for key_f in ALREADY_FOLLOWING_KEYWORDS:
                    if key_f in page_content_lower:
                        already_following = True
                        break

                if already_following:
                    logger.info(f"Omitiendo: {filename_base} ya sigue al objetivo (Detectado por heurística multilingüe).")
                    await browser.close()
                    return True, None, None, False

                # --- ÁRBOL DE DECISIÓN MULTI-SELECTOR DE SEGUIMIENTO MULTIPATRIA ---
                follow_button = None
                
                # Intentamos primero con selectores estructurales globales (data-e2e, clases comunes, etc.)
                structural_selectors = [
                    '[data-e2e="follow-button"]',
                    'button[class*="FollowButton"]',
                    'div[role="button"][class*="follow"]',
                    '.follow-button'
                ]
                
                for sel in structural_selectors:
                    try:
                        element = await page.wait_for_selector(sel, timeout=2500, state="visible")
                        if element:
                            follow_button = element
                            break
                    except PlaywrightTimeout:
                        continue

                # Fallback: Si los estructurales fallan, buscamos de manera dinámica según el idioma del botón en pantalla
                if not follow_button:
                    for key_btn in FOLLOW_KEYWORDS:
                        selectors_fallback = [
                            f'button:has-text("{key_btn.capitalize()}")',
                            f'button:has-text("{key_btn.lower()}")',
                            f'div[role="button"]:has-text("{key_btn.capitalize()}")',
                            f'div[role="button"]:has-text("{key_btn.lower()}")'
                        ]
                        for f_sel in selectors_fallback:
                            try:
                                element = await page.wait_for_selector(f_sel, timeout=1000, state="visible")
                                if element:
                                    follow_button = element
                                    break
                            except PlaywrightTimeout:
                                continue
                        if follow_button:
                            break

                # --- SIMULACIÓN DE CLIC RESIDENCIAL ---
                if follow_button:
                    # Scroll leve imitando navegación humana antes de pulsar
                    await page.evaluate("window.scrollBy(0, window.innerHeight / 4)")
                    await asyncio.sleep(random.uniform(0.6, 1.4))
                    
                    box = await follow_button.bounding_box()
                    if box:
                        await page.mouse.move(box["x"] + box["width"]/2, box["y"] + box["height"]/2)
                        await asyncio.sleep(random.uniform(0.1, 0.4))
                        await page.mouse.click(box["x"] + box["width"]/2, box["y"] + box["height"]/2)
                    else:
                        await follow_button.click()
                        
                    await asyncio.sleep(random.uniform(3.5, 5.0))

                    # --- VERIFICACIÓN DE CONTROL POST-ACCIÓN ---
                    button_text_after = (await follow_button.inner_text()).lower()
                    
                    # Comprobamos si el botón cambió a un estado de "Siguiendo" o "Mensaje" en cualquier idioma
                    action_confirmed = False
                    for key_ok in ALREADY_FOLLOWING_KEYWORDS + MESSAGE_KEYWORDS:
                        if key_ok in button_text_after:
                            action_confirmed = True
                            break
                    
                    if not action_confirmed:
                        # Fallback 2: Intentar forzar inyección Javascript directa de emergencia
                        logger.warning("Verificación fallida. Forzando inyección Javascript de emergencia...")
                        await page.evaluate('(el) => el.click()', follow_button)
                        await asyncio.sleep(3.5)
                        
                        button_text_retry = (await follow_button.inner_text()).lower()
                        retry_confirmed = False
                        for key_ok in ALREADY_FOLLOWING_KEYWORDS + MESSAGE_KEYWORDS:
                            if key_ok in button_text_retry:
                                retry_confirmed = True
                                break
                        
                        if not retry_confirmed:
                            screenshot_file = f"limite_{int(time.time())}.png"
                            await page.screenshot(path=screenshot_file, full_page=True)
                            await browser.close()
                            return False, "Acción de seguir denegada (Límite Diario excedido o cuenta bloqueada).", screenshot_file, False

                    # Éxito de la operación. Actualizamos rendimiento del proxy usado
                    if proxy:
                        async with db_lock:
                            try:
                                conn = sqlite3.connect(DB_FILE, timeout=5.0)
                                cursor = conn.cursor()
                                cursor.execute(
                                    "INSERT OR REPLACE INTO proxy_rendimiento (proxy, exitos, fallos) VALUES (?, COALESCE((SELECT exitos FROM proxy_rendimiento WHERE proxy=?)+1, 1), COALESCE((SELECT fallos FROM proxy_rendimiento WHERE proxy=?), 0))",
                                    (proxy, proxy, proxy)
                                )
                                conn.commit()
                                conn.close()
                            except Exception as dberr:
                                logger.error(f"Fallo al registrar éxito de proxy: {dberr}")

                    await browser.close()
                    return True, None, None, False
                else:
                    screenshot_file = f"no_btn_{int(time.time())}.png"
                    await page.screenshot(path=screenshot_file, full_page=True)
                    await browser.close()
                    return False, "No se localizó el botón de Seguir en ningún idioma.", screenshot_file, False

            except Exception as e:
                screenshot_file = f"error_sistema_{int(time.time())}.png"
                try:
                    await page.screenshot(path=screenshot_file, full_page=True)
                except Exception:
                    screenshot_file = None
                
                await browser.close()
                error_msg = str(e)
                is_network_error = "net::" in error_msg or "Timeout" in error_msg or "proxy" in error_msg.lower() or "DNS_" in error_msg
                
                if proxy:
                    async with db_lock:
                        try:
                            conn = sqlite3.connect(DB_FILE, timeout=5.0)
                            cursor = conn.cursor()
                            cursor.execute(
                                "INSERT OR REPLACE INTO proxy_rendimiento (proxy, exitos, fallos) VALUES (?, COALESCE((SELECT exitos FROM proxy_rendimiento WHERE proxy=?), 0), COALESCE((SELECT fallos FROM proxy_rendimiento WHERE proxy=?)+1, 1))",
                                (proxy, proxy, proxy)
                            )
                            conn.commit()
                            conn.close()
                        except Exception as dberr:
                            logger.error(f"Fallo al registrar error de proxy: {dberr}")

                return False, f"Fallo de conexión o timeout: {error_msg[:45]}...", screenshot_file, is_network_error
            finally:
                gc.collect()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manejador del comando inicial /start del panel de control de Telegram."""
    welcome_msg = (
        "👑 *Ishak SMM Control Center - V3 ULTIMATE* 👑\n\n"
        "Sistema de automatización masiva con soporte de cookies multilingües, "
        "detección de estado global y rotación inteligente de proxies.\n\n"
        "⚡ *Comandos Disponibles:*\n"
        "👉 `/follow <link_tiktok> <cantidad>` - Iniciar envío inteligente\n"
        "👉 `/check` - Verificar salud de todas tus cookies en segundo plano\n"
        "👉 `/proxycheck` - Benchmarking de latencia y estado de tus proxies\n"
        "👉 `/report` - Reporte analítico de rendimiento global\n"
        "👉 `/stats` - Métricas rápidas de inventario\n"
        "👉 `/clean` - Purga del historial de errores"
    )
    await update.message.reply_text(welcome_msg, parse_mode='Markdown')

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra estadísticas rápidas, conteo de cookies saludables y muertas."""
    cookies_validas = len([f for f in os.listdir(COOKIES_DIR) if f.endswith(".txt")])
    cookies_muertas = len([f for f in os.listdir(BAD_COOKIES_DIR) if f.endswith(".txt")])
    proxies_totales = len(load_proxies())
    
    async with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10.0)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(DISTINCT target_url) FROM seguimientos")
            objetivos_totales = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM seguimientos")
            total_hits = cursor.fetchone()[0]
            conn.close()
        except Exception as e:
            logger.error(f"Error consultando estadísticas en DB: {e}")
            objetivos_totales, total_hits = 0, 0

    stats_msg = (
        "📊 *Ishak SMM - Inventario Operacional* 📊\n\n"
        f"🎯 *Perfiles Procesados:* `{objetivos_totales}`\n"
        f"✅ *Hits Exitosos:* `{total_hits}` follows verificados\n\n"
        f"📂 *Inventario de Cuentas:*\n"
        f"🟢 *Cuentas Activas:* `{cookies_validas}`\n"
        f"🔴 *Cuentas Expiradas:* `{cookies_muertas}`\n"
        f"🌐 *Proxies Registradas:* `{proxies_totales}`"
    )
    await update.message.reply_text(stats_msg, parse_mode='Markdown')

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ejecuta una verificación de salud asíncrona de todas las cookies cargadas sin ejecutar follows."""
    todas_cookies = [f for f in os.listdir(COOKIES_DIR) if f.endswith(".txt")]
    if not todas_cookies:
        await update.message.reply_text("❌ No hay archivos de cookies `.txt` en la carpeta `cookies/`.")
        return

    status_msg = await update.message.reply_text(f"🔍 *Iniciando diagnóstico asíncrono de {len(todas_cookies)} cookies...*", parse_mode='Markdown')
    
    proxies = load_proxies()
    semaphore = asyncio.Semaphore(2)  # Verificación paralela regulada para no agotar recursos
    correctas = 0
    erroneas = 0

    async def verify_cookie_task(idx, cookie_file):
        nonlocal correctas, erroneas
        cookie_path = os.path.join(COOKIES_DIR, cookie_file)
        proxy = proxies[idx % len(proxies)] if proxies else None
        
        async with semaphore:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu', '--single-process'])
                cookie_data = parse_netscape_cookies(cookie_path)
                if not cookie_data:
                    erroneas += 1
                    await browser.close()
                    return

                try:
                    user_agent, viewport = get_random_fingerprint()
                    ctx = await browser.new_context(
                        proxy={"server": proxy} if proxy else None, 
                        storage_state=cookie_data, 
                        user_agent=user_agent, 
                        viewport=viewport,
                        locale="en-US"
                    )
                    page = await ctx.new_page()
                    page.set_default_timeout(15000)
                    
                    await page.goto("https://www.tiktok.com/foryou", wait_until="domcontentloaded")
                    await asyncio.sleep(2.0)
                    
                    login_btn = await page.query_selector('button[data-e2e="top-login-button"]')
                    if login_btn:
                        erroneas += 1
                        try:
                            shutil.move(cookie_path, os.path.join(BAD_COOKIES_DIR, cookie_file))
                        except Exception:
                            pass
                    else:
                        correctas += 1
                except Exception:
                    erroneas += 1
                finally:
                    await browser.close()

    tasks = [asyncio.create_task(verify_cookie_task(i, file)) for i, file in enumerate(todas_cookies)]
    await asyncio.gather(*tasks)

    await status_msg.edit_text(
        f"🏁 *Diagnóstico de Cookies Finalizado* 🏁\n\n"
        f"🟢 *Cuentas Operativas (Sanas):* `{correctas}`\n"
        f"🔴 *Cuentas Expiradas (Movidas):* `{erroneas}`\n\n"
        f"_Nota: Las cuentas marcadas como muertas se han movido automáticamente a la carpeta `/cookies_caducadas`._",
        parse_mode='Markdown'
    )

async def proxy_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando asíncrono para testear de verdad la latencia y funcionamiento de cada proxy."""
    proxies = load_proxies()
    if not proxies:
        await update.message.reply_text("❌ No se encontró el archivo `proxies.txt` o está vacío.")
        return

    status_msg = await update.message.reply_text(f"⚡ *Probando conexión y latencia de {len(proxies)} proxies contra TikTok...*", parse_mode='Markdown')
    
    funcionan = []
    caidas = []

    async def test_single_proxy(proxy):
        try:
            start_time = time.time()
            async with httpx.AsyncClient(proxies={"all://": proxy}, timeout=6.0) as client:
                response = await client.get("https://www.tiktok.com", headers={"User-Agent": "Mozilla/5.0"})
                if response.status_code < 400:
                    latencia = (time.time() - start_time) * 1000
                    funcionan.append((proxy, latencia))
                else:
                    caidas.append(proxy)
        except Exception:
            caidas.append(proxy)

    await asyncio.gather(*(test_single_proxy(p) for p in proxies))

    # Guardar en base de datos el benchmark
    async with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=5.0)
            cursor = conn.cursor()
            for proxy, lat in funcionan:
                cursor.execute(
                    "INSERT OR REPLACE INTO proxy_rendimiento (proxy, latencia) VALUES (?, ?)", (proxy, lat)
                )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error escribiendo benchmark en base de datos: {e}")

    # Ordenar por menor latencia
    funcionan.sort(key=lambda x: x[1])
    report_text = f"🌐 *Benchmark de Red Completado* 🌐\n\n" \
                  f"✅ *Proxies Optimizadas:* `{len(funcionan)}` funcionales\n" \
                  f"❌ *Proxies Caídas:* `{len(caidas)}` sin respuesta\n\n"
    
    if funcionan:
        report_text += "🚀 *Las 5 mejores proxies (Menor Latencia):*\n"
        for p, lat in funcionan[:5]:
            report_text += f"• `{p.split('://')[-1]}` → `{lat:.0f}ms` ⚡\n"
            
    await status_msg.edit_text(report_text, parse_mode='Markdown')

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Genera un reporte analítico directamente desde la base de datos de rendimiento."""
    async with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10.0)
            cursor = conn.cursor()
            
            cursor.execute("SELECT COUNT(*) FROM seguimientos")
            total_hits = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(DISTINCT target_url) FROM seguimientos")
            total_targets = cursor.fetchone()[0]

            cursor.execute("SELECT cookie_file, COUNT(*) as c FROM seguimientos GROUP BY cookie_file ORDER BY c DESC LIMIT 5")
            top_cuentas = cursor.fetchall()

            cursor.execute("SELECT proxy, exitos, fallos, latencia FROM proxy_rendimiento ORDER BY exitos DESC, latencia ASC LIMIT 3")
            top_proxies = cursor.fetchall()

            conn.close()
        except Exception as e:
            logger.error(f"Error extrayendo reporte de DB: {e}")
            await update.message.reply_text("❌ Error al acceder a los datos históricos.")
            return

    report_msg = (
        "👑 *SMM Performance Dashboard* 👑\n\n"
        f"📈 *Total Seguidos Completados:* `{total_hits}`\n"
        f"🎯 *Perfiles Promocionados:* `{total_targets}`\n\n"
        "👤 *Top 5 Cuentas Más Activas:*\n"
    )
    for index, (file, counts) in enumerate(top_cuentas, 1):
        report_msg += f"{index}. `{file}` → `{counts} follows` ✅\n"
    if not top_cuentas:
        report_msg += "_Sin datos de uso aún._\n"

    report_msg += "\n🌐 *Top 3 Proxies Más Robustas:*\n"
    for index, (proxy, exitos, fallos, lat) in enumerate(top_proxies, 1):
        clean_proxy = proxy.split("://")[-1]
        report_msg += f"{index}. `{clean_proxy}` → `{exitos} OK` / `{fallos} ERR` ({lat:.0f}ms)\n"
    if not top_proxies:
        report_msg += "_Sin historial de red mapeado._\n"

    await update.message.reply_text(report_msg, parse_mode='Markdown')

async def clean_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Limpia registros de fallos temporales de cookies para reincorporación en el pool."""
    async with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10.0)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM cookies_estado")
            cursor.execute("DELETE FROM proxy_rendimiento")
            conn.commit()
            conn.close()
            await update.message.reply_text("🧼 *Limpieza ejecutada:* Se ha reseteado el historial de cookies fallidas de la base de datos.", parse_mode='Markdown')
        except Exception as e:
            await update.message.reply_text(f"❌ Error al limpiar base de datos: {e}")

async def follow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Controlador maestro de campañas. Distribuye el trabajo entre múltiples browsers simultáneos,
    evitando duplicidades de uso de cookies en el mismo target y rotando proxies en caso de fallo.
    """
    if len(context.args) < 2:
        await update.message.reply_text("⚠️ *Sintaxis incorrecta.* Usa: `/follow <link_tiktok> <cantidad>`", parse_mode='Markdown')
        return

    raw_url = context.args[0]
    try:
        cantidad_solicitada = int(context.args[1])
    except ValueError:
        await update.message.reply_text("⚠️ La cantidad especificada debe ser un valor numérico entero.")
        return

    resolving_msg = await update.message.reply_text("🔍 *Analizando y resolviendo enlace de TikTok...*", parse_mode='Markdown')
    target_url = await resolve_tiktok_url(raw_url)
    
    proxies = load_proxies()
    todas_cookies = [f for f in os.listdir(COOKIES_DIR) if f.endswith(".txt")]
    
    if not todas_cookies:
        await resolving_msg.edit_text("❌ *Error:* No hay archivos de sesión `.txt` en la carpeta `/cookies`.")
        return

    # Filtrar cookies que ya hayan seguido a este objetivo previamente en base de datos
    async with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10.0)
            cursor = conn.cursor()
            cursor.execute("SELECT cookie_file FROM seguimientos WHERE target_url = ?", (target_url,))
            ya_siguen = [row[0] for row in cursor.fetchall()]
            conn.close()
        except Exception as e:
            logger.error(f"Error consultando historial de cookies: {e}")
            ya_siguen = []

    cookies_disponibles = [cookie for cookie in todas_cookies if cookie not in ya_siguen]
    
    if not cookies_disponibles:
        await resolving_msg.edit_text("⚠️ *Control de Duplicidad:* Todas las cuentas válidas ya están siguiendo a este perfil.")
        return

    cantidad_realizar = min(cantidad_solicitada, len(cookies_disponibles))
    
    await resolving_msg.edit_text(
        f"🚀 *Iniciando campaña de automatización...*\n\n"
        f"🎯 *Perfil:* `{target_url}`\n"
        f"📦 *Objetivo:* `{cantidad_realizar}` seguidores reales\n"
        f"⏳ *Estado:* Preparando hilos concurrentes...",
        parse_mode='Markdown'
    )

    # Concurrencia balanceada de hilos para optimizar consumo en Render
    max_concurrent_browsers = 3
    semaphore = asyncio.Semaphore(max_concurrent_browsers)
    
    exitos = 0
    fallos = 0
    inicio_time = time.time()
    last_edit_time = 0

    async def worker_task(idx, cookie_file):
        nonlocal exitos, fallos, last_edit_time
        cookie_path = os.path.join(COOKIES_DIR, cookie_file)
        proxy = proxies[idx % len(proxies)] if proxies else None
        
        intentos = 2
        for intento in range(intentos):
            success, error_msg, path_img, is_network_error = await perform_action(
                proxy, cookie_path, target_url, semaphore
            )
            
            if success:
                exitos += 1
                async with db_lock:
                    try:
                        conn = sqlite3.connect(DB_FILE, timeout=10.0)
                        cursor = conn.cursor()
                        cursor.execute(
                            "INSERT INTO seguimientos (target_url, cookie_file) VALUES (?, ?)", 
                            (target_url, cookie_file)
                        )
                        conn.commit()
                        conn.close()
                    except Exception as e:
                        logger.error(f"Error al escribir éxito en base de datos: {e}")
                break
            
            else:
                if is_network_error and intento < intentos - 1 and proxies:
                    logger.warning(f"Error de red/proxy detectado en el intento {intento + 1} para {cookie_file}. Rotando proxy...")
                    proxy = random.choice(proxies)
                    if path_img and os.path.exists(path_img):
                        try:
                            os.remove(path_img)
                        except Exception:
                            pass
                    continue
                
                fallos += 1
                async with db_lock:
                    try:
                        conn = sqlite3.connect(DB_FILE, timeout=10.0)
                        cursor = conn.cursor()
                        cursor.execute(
                            "INSERT OR REPLACE INTO cookies_estado (cookie_file, estado, motivo) VALUES (?, ?, ?)",
                            (cookie_file, "INVALIDA", error_msg)
                        )
                        conn.commit()
                        conn.close()
                    except Exception as e:
                        logger.error(f"Error al escribir estado de cookie en base de datos: {e}")

                if "caducada" in error_msg.lower() or "sesión" in error_msg.lower() or "login" in error_msg.lower():
                    try:
                        shutil.move(cookie_path, os.path.join(BAD_COOKIES_DIR, cookie_file))
                    except Exception as e:
                        logger.error(f"No se pudo mover el archivo de cookie expirado: {e}")

                if path_img and os.path.exists(path_img):
                    try:
                        await context.bot.send_photo(
                            chat_id=update.effective_chat.id,
                            photo=open(path_img, 'rb'),
                            caption=f"⚠️ *Fallo Crítico en Cuenta:* `{cookie_file}`\n\n🚫 *Causa:* `{error_msg}`\n🔧 *Acción:* Archivo aislado de la cola activa.",
                            parse_mode='Markdown'
                        )
                    except Exception as telegram_err:
                        logger.error(f"Error al enviar reporte de error a Telegram: {telegram_err}")
                    finally:
                        try:
                            os.remove(path_img)
                        except Exception:
                            pass
                break

        # Actualización de la barra de progreso
        procesados = exitos + fallos
        porcentaje = int((procesados / cantidad_realizar) * 10)
        barra_progreso = "█" * porcentaje + "░" * (10 - porcentaje)
        
        tiempo_transcurrido = time.time() - inicio_time
        velocidad_estimada = (procesados / tiempo_transcurrido) * 60 if tiempo_transcurrido > 0 else 0
        
        tiempo_actual = time.time()
        if (tiempo_actual - last_edit_time > 3.5) or (procesados == cantidad_realizar):
            try:
                await resolving_msg.edit_text(
                    f"⚡ *Ejecución de Campaña en Progreso...*\n\n"
                    f"🎯 *Objetivo:* {target_url}\n"
                    f"📊 *Progreso:* `[{barra_progreso}]` {procesados}/{cantidad_realizar}\n\n"
                    f"🟢 *Éxitos:* `{exitos}` | 🔴 *Fallas:* `{fallos}`\n"
                    f"⏱️ *Rendimiento:* `{velocidad_estimada:.1f} follows/min`",
                    parse_mode='Markdown'
                )
                last_edit_time = tiempo_actual
            except BadRequest as br:
                if "Message is not modified" not in str(br):
                    logger.error(f"Error al editar mensaje en Telegram: {br}")
            except Exception as ex:
                logger.error(f"Excepción general en edición de estado: {ex}")

    # Crear tareas y controlar la finalización de todos los hilos
    tasks = []
    for i in range(cantidad_realizar):
        tasks.append(asyncio.create_task(worker_task(i, cookies_disponibles[i])))
        
    await asyncio.gather(*tasks)
    
    tiempo_total = time.time() - inicio_time
    await update.message.reply_text(
        f"🏁 *Campaña de Seguidores Finalizada* 🏁\n\n"
        f"📊 *Estadísticas de la Campaña:*\n"
        f"👤 *Objetivo:* `{target_url}`\n"
        f"✅ *Fórmula Exitosa:* `{exitos}/{cantidad_realizar}` cuentas\n"
        f"❌ *Cuentas Rechazadas/Expiradas:* `{fallos}`\n"
        f"⏱️ *Tiempo Invertido:* `{tiempo_total:.1f} segundos`",
        parse_mode='Markdown'
    )

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("proxycheck", proxy_check_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("follow", follow_command))
    app.add_handler(CommandHandler("clean", clean_command))
    
    print("🤖 Servidor central del SMM TikTok Automation Bot activo y escuchando eventos...")
    app.run_polling()
