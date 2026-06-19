import os
import re
import sys
import time
import sqlite3
import random
import asyncio
import logging
import threading
import http.server
import socketserver
from pathlib import Path

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

# ==========================================
# CONFIGURACIÓN GENERAL Y LOGGING
# ==========================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Estados del flujo de conversación del Bot
AWAITING_QUANTITY, AWAITING_LINK = range(2)

DB_PATH = "smm_bot.db"
COOKIES_DIR = Path("cookies")
EXPIRED_COOKIES_DIR = Path("cookies_caducadas")
PROXIES_FILE = Path("proxies.txt")

# Aseguramos la existencia de directorios básicos
COOKIES_DIR.mkdir(exist_ok=True)
EXPIRED_COOKIES_DIR.mkdir(exist_ok=True)
if not PROXIES_FILE.exists():
    PROXIES_FILE.touch()

# Bloqueo global de base de datos para evitar colisiones asíncronas
db_lock = asyncio.Lock()

# Diccionarios multilingües para selectores de TikTok
FOLLOW_KEYWORDS = ["follow", "seguir", "s'abonner", "seguir también", "follow back", "подписаться", "ติดตาม", "theo dõi", "suivre", "folgen", "takip et"]
ALREADY_FOLLOWING_KEYWORDS = ["following", "siguiendo", "abonné", "mutual", "amigos", "message", "mensaje", "enviar mensaje", "messages", "сообщение", "ส่งข้อความ", "tin nhắn", "nachricht"]

# ==========================================
# SERVIDOR WEB DE DIAGNÓSTICO (KEEP-ALIVE)
# ==========================================
def iniciar_servidor_ping():
    """Levanta un servidor HTTP ligero para pasar el Health Check de Render en Web Services."""
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            pass # Evitamos saturar los logs de Render
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"SMM Bot Activo y Corriendo")

    def run_server():
        port = int(os.environ.get("PORT", 8080))
        logger.info(f"🛰️ Iniciando servidor web de diagnóstico en el puerto {port}")
        try:
            with socketserver.TCPServer(("", port), QuietHandler) as httpd:
                httpd.serve_forever()
        except Exception as e:
            logger.error(f"Error al iniciar el servidor de ping: {e}")

    threading.Thread(target=run_server, daemon=True).start()

# ==========================================
# TRADUCTOR DE COOKIES NETSCAPE (.TXT -> DICT)
# ==========================================
def parsear_netscape_cookies(filepath: Path) -> list:
    """Parsea archivos de cookies en formato Netscape (.txt) y los adapta a Playwright."""
    cookies = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        for line in content.splitlines():
            line = line.strip()
            # Ignoramos metadatos de checkers y comentarios tradicionales
            if not line or line.startswith("#") or line.startswith("–") or line.startswith("Simple"):
                continue
            
            parts = line.split("\t")
            if len(parts) >= 7:
                domain = parts[0]
                if "tiktok.com" not in domain:
                    continue
                
                secure = parts[1].upper() == "TRUE"
                path = parts[2]
                http_only = parts[3].upper() == "TRUE"
                
                try:
                    expires = float(parts[4])
                except ValueError:
                    expires = -1.0

                name = parts[5]
                value = parts[6]

                cookies.append({
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path,
                    "secure": secure,
                    "httpOnly": http_only,
                    "expires": expires,
                    "sameSite": "Lax"
                })
    except Exception as e:
        logger.error(f"Error parseando cookies de {filepath.name}: {e}")
    return cookies

# ==========================================
# GESTIÓN DE BASE DE DATOS SQLITE
# ==========================================
def inicializar_db():
    """Crea las tablas de control si no existen."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cookies (
            id TEXT PRIMARY KEY,
            username TEXT,
            status TEXT DEFAULT 'active',
            last_used TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tracking_seguimiento (
            cookie_id TEXT,
            target_profile TEXT,
            followed_at TIMESTAMP,
            PRIMARY KEY (cookie_id, target_profile)
        )
    """)
    conn.commit()
    conn.close()

async def registrar_seguimiento(cookie_id: str, target: str):
    async with db_lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO tracking_seguimiento VALUES (?, ?, ?)",
            (cookie_id, target, time.strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        conn.close()

async def obtener_cookies_disponibles(target_profile: str) -> list:
    """Filtra y devuelve las cookies que no han seguido todavía a este perfil objetivo."""
    async with db_lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # Buscamos archivos .txt reales en el directorio
        archivos_cookies = [f.name for f in COOKIES_DIR.glob("*.txt")]
        
        # Sincronizamos la base de datos con los archivos reales existentes
        for archivo in archivos_cookies:
            cursor.execute("INSERT OR IGNORE INTO cookies (id, status) VALUES (?, 'active')", (archivo,))
        conn.commit()

        # Seleccionamos las que estén marcadas como activas y que no existan en el historial de este perfil
        cursor.execute("""
            SELECT id FROM cookies 
            WHERE status = 'active' 
            AND id NOT IN (
                SELECT cookie_id FROM tracking_seguimiento WHERE target_profile = ?
            )
            ORDER BY last_used ASC
        """, (target_profile,))
        
        filas = cursor.fetchall()
        conn.close()
        return [f[0] for f in filas]

async def marcar_cookie_estado(cookie_id: str, estado: str):
    async with db_lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE cookies SET status = ?, last_used = ? WHERE id = ?",
            (estado, time.strftime('%Y-%m-%d %H:%M:%S'), cookie_id)
        )
        conn.commit()
        conn.close()

# ==========================================
# RUTINA DE REDIRECCIÓN DE ENLACES
# ==========================================
async def resolver_enlace_tiktok(url_original: str) -> str:
    """Resuelve enlaces tipo vm.tiktok.com o vt.tiktok.com a sus URLs reales de escritorio."""
    if "tiktok.com" not in url_original:
        return url_original
    if any(pattern in url_original for pattern in ["vm.tiktok.com", "vt.tiktok.com", "v.tiktok.com"]):
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                response = await client.head(url_original, headers=headers)
                url_limpia = str(response.url).split("?")[0]
                return url_limpia
        except Exception as e:
            logger.error(f"Error resolviendo redirección de enlace: {e}")
    return url_original

# ==========================================
# MOTOR DE AUTOMATIZACIÓN (PLAYWRIGHT)
# ==========================================
async def realizar_seguimiento_individual(cookie_id: str, target_url: str, proxy: str) -> bool:
    """Abre el navegador con Playwright, inyecta cookies, navega y realiza el seguimiento."""
    cookies_list = parsear_netscape_cookies(COOKIES_DIR / cookie_id)
    if not cookies_list:
        logger.warning(f"La cookie {cookie_id} está vacía o corrupta.")
        await marcar_cookie_estado(cookie_id, "dead")
        mover_cookie_caducada(cookie_id)
        return False

    # Configuración de proxies para Playwright
    proxy_opt = None
    if proxy:
        proxy_opt = {"server": proxy}

    # Spoofing de User Agent aleatorio
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0"
    ]

    async with async_playwright() as p:
        browser = None
        try:
            # OPTIMIZACIONES CRÍTICAS PARA DOCKER/RENDER:
            # --disable-dev-shm-usage evita congelamientos por límite de memoria compartida en contenedores
            # --disable-gpu ahorra consumo de procesamiento en servidores sin tarjeta gráfica
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled"
                ]
            )
            
            # Forzamos localización en inglés para estandarizar los layouts de botones
            context = await browser.new_context(
                user_agent=random.choice(user_agents),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="America/New_York",
                proxy=proxy_opt
            )

            await stealth_async(context)
            await context.add_cookies(cookies_list)

            page = await context.new_page()
            
            # Reducimos el timeout por defecto para evitar bloqueos largos (20 segundos)
            page.set_default_timeout(20000)

            logger.info(f"Abriendo perfil {target_url} con la cuenta {cookie_id}")
            # Definimos un timeout estricto de carga directamente en la navegación
            await page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(random.randint(2000, 4000))

            # Verificar si nos encontramos con un Captcha o bloqueo de seguridad
            title = await page.title()
            if "security verification" in title.lower() or await page.locator("div.captcha-container").count() > 0:
                logger.warning(f"¡Captcha detectado en {cookie_id}!")
                await marcar_cookie_estado(cookie_id, "captcha")
                return False

            # Verificar primero si el botón de seguir ya está en estado "Seguido"
            texto_pagina = (await page.content()).lower()
            ya_sigue = any(keyword in texto_pagina for keyword in ALREADY_FOLLOWING_KEYWORDS)
            
            # Buscamos botones de seguir genéricos
            botones_seguir = page.locator("button")
            boton_encontrado = None
            
            for i in range(await botones_seguir.count()):
                btn = botones_seguir.nth(i)
                text = (await btn.text_content() or "").strip().lower()
                data_e2e = await btn.get_attribute("data-e2e") or ""

                # Priorizamos selectores estructurados
                if "follow-button" in data_e2e or any(kw == text for kw in FOLLOW_KEYWORDS):
                    boton_encontrado = btn
                    break

            if ya_sigue and not boton_encontrado:
                logger.info(f"La cuenta {cookie_id} ya seguía al perfil {target_url}.")
                await registrar_seguimiento(cookie_id, target_url)
                return True

            if not boton_encontrado:
                # Comprobación mediante inyección JS si fallan los selectores típicos
                logger.warning(f"No se detectó el botón de seguir con selectores en {cookie_id}. Intentando JS de emergencia.")
                await page.evaluate("""
                    const btns = document.querySelectorAll('button');
                    for (const btn of btns) {
                        const txt = btn.textContent.toLowerCase().trim();
                        if (txt === 'follow' || txt === 'seguir' || btn.getAttribute('data-e2e') === 'follow-button') {
                            btn.click();
                            break;
                        }
                    }
                """)
            else:
                # Comportamiento humano antes del click
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 6)")
                await page.wait_for_timeout(random.randint(500, 1500))
                await boton_encontrado.click()
                logger.info(f"Click efectuado con {cookie_id}.")

            await page.wait_for_timeout(3000)

            # Verificación de efectividad real de la suscripción (Anti-Shadowban)
            html_final = (await page.content()).lower()
            verificado = any(kw in html_final for kw in ALREADY_FOLLOWING_KEYWORDS)

            if verificado:
                logger.info(f"Seguimiento exitoso verificado en {cookie_id}!")
                await registrar_seguimiento(cookie_id, target_url)
                await marcar_cookie_estado(cookie_id, "active")
                return True
            else:
                logger.warning(f"Fallo de verificación de suscripción en {cookie_id}.")
                return False

        except Exception as e:
            logger.error(f"Error procesando seguimiento con {cookie_id}: {e}")
            return False
        finally:
            if browser:
                await browser.close()

def mover_cookie_caducada(cookie_id: str):
    """Mueve una cookie del directorio activo al de caducadas de forma segura."""
    origen = COOKIES_DIR / cookie_id
    destino = EXPIRED_COOKIES_DIR / cookie_id
    try:
        if origen.exists():
            origen.replace(destino)
            logger.info(f"Moviendo cookie {cookie_id} a cookies_caducadas.")
    except Exception as e:
        logger.error(f"No se pudo mover la cookie {cookie_id}: {e}")

# ==========================================
# CARGA DE PROXIES
# ==========================================
def obtener_lista_proxies() -> list:
    if not PROXIES_FILE.exists():
        return []
    with open(PROXIES_FILE, "r") as f:
        return [line.strip() for line in f if line.strip()]

# ==========================================
# CONTROLADOR DE PROGRESO DE CAMPAÑA (ANTI-FLOOD)
# ==========================================
class MonitorProgreso:
    def __init__(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, total: int):
        self.context = context
        self.chat_id = chat_id
        self.message_id = message_id
        self.total = total
        self.actual = 0
        self.exitos = 0
        self.fallidos = 0
        self.last_update = 0
        self.lock = asyncio.Lock()

    async def registrar_resultado(self, exito: bool):
        async with self.lock:
            self.actual += 1
            if exito:
                self.exitos += 1
            else:
                self.fallidos += 1

            ahora = time.time()
            # Telegram limita a 1 edición cada 3 segundos por chat para evitar FloodWait
            if ahora - self.last_update >= 3.0 or self.actual == self.total:
                self.last_update = ahora
                porcentaje = int((self.actual / self.total) * 100)
                longitud_barra = 10
                relleno = int(porcentaje / 10)
                barra = "█" * relleno + "░" * (longitud_barra - relleno)

                text = (
                    f"📊 **Progreso de la Campaña de Seguidores**\n\n"
                    f"├ Estado: {barra} {porcentaje}%\n"
                    f"├ Completado: `{self.actual}` de `{self.total}`\n"
                    f"├ ✅ Éxitos: `{self.exitos}`\n"
                    f"└ ❌ Fallidos: `{self.fallidos}`\n\n"
                    f"⏰ _No cierres este chat, estamos procesando las peticiones en segundo plano._"
                )
                try:
                    await self.context.bot.edit_message_text(
                        chat_id=self.chat_id,
                        message_id=self.message_id,
                        text=text,
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.debug(f"Error al editar progreso de Telegram: {e}")

# ==========================================
# MANEJADORES DE COMANDOS DEL BOT
# ==========================================
async def comando_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el menú principal con botones interactivos."""
    # Reseteamos cualquier estado del usuario por si acaso
    context.user_data.clear()
    
    # Intentamos detectar si es Ishak para personalizar el saludo
    nombre = update.effective_user.first_name
    saludo = f"¡Hola {nombre}! 👋"
    if "ishak" in nombre.lower() or update.effective_user.id == 5000000000: # Opcional ID
        saludo = "¡Qué pasa Ishak! 👑 Bienvenido de nuevo al cuartel general del Ishak Empire."

    texto = (
        f"{saludo}\n"
        f"Este es tu bot SMM Profesional de TikTok.\n"
        f"Usa el panel interactivo de abajo para controlarlo todo de forma segura."
    )

    teclado = [
        [InlineKeyboardButton("🚀 Iniciar Campaña", callback_data="btn_iniciar")],
        [
            InlineKeyboardButton("🔄 Verificar Cookies", callback_data="btn_check"),
            InlineKeyboardButton("📡 Testear Proxies", callback_data="btn_proxies"),
        ],
        [InlineKeyboardButton("📈 Reporte de Cuentas", callback_data="btn_report")],
    ]
    reply_markup = InlineKeyboardMarkup(teclado)

    await update.message.reply_text(texto, reply_markup=reply_markup)

async def callback_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestor de clicks de los botones del menú principal."""
    query = update.callback_query
    await query.answer()

    if query.data == "btn_iniciar":
        await query.message.reply_text(
            "🚀 **Nueva Campaña de Seguidores**\n\n"
            "Introduce la cantidad exacta de seguidores que deseas enviar.\n"
            "_(Debe ser un número entero mayor a 0, ej: 10)_",
            parse_mode="Markdown"
        )
        return AWAITING_QUANTITY

    elif query.data == "btn_check":
        await query.message.reply_text("⏳ Iniciando análisis de cookies en segundo plano...")
        await analizar_estado_cookies(query.message)
        
    elif query.data == "btn_proxies":
        await query.message.reply_text("⏳ Iniciando test de latencia de proxies...")
        await testear_lista_proxies(query.message)

    elif query.data == "btn_report":
        await generar_reporte_stats(query.message)

# ==========================================
# CONVERSACIÓN: REGISTRO DE SEGUIDORES
# ==========================================
async def recibir_cantidad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe la cantidad de seguidores y valida estrictamente el número entero."""
    texto = update.message.text.strip()

    # Si es un comando, cancelamos la conversación actual y lo procesamos directamente
    if texto.startswith("/"):
        await update.message.reply_text("❌ Operación abortada. Se ha cancelado el asistente para procesar tu comando.")
        context.user_data.clear()
        if texto == "/start":
            await comando_start(update, context)
        return ConversationHandler.END

    if not texto.isdigit() or int(texto) <= 0:
        await update.message.reply_text(
            "❌ **Error:** La cantidad especificada debe ser un valor numérico entero positivo.\n\n"
            "Escribe un número entero (Ej: `15`) o envía `/cancel` para volver al menú.",
            parse_mode="Markdown"
        )
        return AWAITING_QUANTITY

    context.user_data["cantidad_pedida"] = int(texto)
    await update.message.reply_text(
        "✅ Cantidad guardada.\n\n"
        "Ahora, por favor, introduce el **enlace de la cuenta de TikTok** objetivo:\n"
        "_(Ej: https://www.tiktok.com/@usuario o https://vm.tiktok.com/xxxxxx/)_",
        parse_mode="Markdown"
    )
    return AWAITING_LINK

async def recibir_enlace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe el enlace de TikTok, lo valida y arranca el procesamiento asíncrono."""
    link = update.message.text.strip()

    # Si es un comando, cancelamos
    if link.startswith("/"):
        await update.message.reply_text("❌ Operación abortada.")
        context.user_data.clear()
        if link == "/start":
            await comando_start(update, context)
        return ConversationHandler.END

    if "tiktok.com" not in link:
        await update.message.reply_text(
            "❌ Enlace inválido. Asegúrate de que sea un enlace de TikTok.\n"
            "Introduce el enlace correcto o envía `/cancel` para abortar.",
            parse_mode="Markdown"
        )
        return AWAITING_LINK

    cantidad = context.user_data["cantidad_pedida"]
    await update.message.reply_text("🔄 Resolviendo enlace de TikTok y preparando campaña asíncrona...")

    # Lanzamos el proceso en segundo plano para no congelar el chat de Telegram
    asyncio.create_task(iniciar_campana_smm(update, context, link, cantidad))
    
    # Finalizamos la conversación para que el bot vuelva a estar disponible al instante
    context.user_data.clear()
    return ConversationHandler.END

async def comando_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancela la conversación actual y vuelve al menú."""
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Asistente cancelado correctamente. Envía `/start` para ver el menú principal."
    )
    return ConversationHandler.END

# ==========================================
# ORQUESTADOR PRINCIPAL DE LA CAMPAÑA SMM
# ==========================================
async def iniciar_campana_smm(update: Update, context: ContextTypes.DEFAULT_TYPE, link: str, cantidad: int):
    chat_id = update.effective_chat.id
    
    # 1. Resolvemos redirecciones (ej. vm.tiktok.com)
    url_final = await resolver_enlace_tiktok(link)
    
    # 2. Obtenemos las cookies disponibles en base de datos para este perfil
    cookies_disponibles = await obtener_cookies_disponibles(url_final)
    proxies = obtener_lista_proxies()

    if not cookies_disponibles:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ **Campaña cancelada:** Todas tus cuentas ya están siguiendo a este usuario, o no hay cookies activas en la carpeta `/cookies`."
        )
        return

    # Ajustamos la cantidad si el pedido supera las cuentas reales libres que tenemos
    cantidad_real = min(cantidad, len(cookies_disponibles))
    cookies_a_usar = cookies_disponibles[:cantidad_real]

    msg_progreso = await context.bot.send_message(
        chat_id=chat_id,
        text=f"🚀 **Campaña iniciada para {url_final}**\n\nPreparando hilos concurrentes seguros..."
    )

    monitor = MonitorProgreso(context, chat_id, msg_progreso.message_id, cantidad_real)
    
    # Limitamos los hilos concurrentes para evitar colgar la RAM de Render (Máximo 3 navegadores simultáneos)
    semaforo = asyncio.Semaphore(3)

    async def tarea_worker(cookie_id):
        async with semaforo:
            exito = False
            # Intentamos con proxies aleatorios seleccionados de forma inteligente
            intentos_proxy = random.sample(proxies, min(3, len(proxies))) if proxies else [None]
            
            for p_url in intentos_proxy:
                logger.info(f"Iniciando intento de seguimiento para {cookie_id} usando proxy: {p_url}")
                try:
                    # BLINDAJE ASÍNCRONO CONTRA CONGELAMIENTOS: 
                    # Forzamos un tiempo de espera estricto de 45 segundos para toda la rutina de Playwright
                    exito = await asyncio.wait_for(
                        realizar_seguimiento_individual(cookie_id, url_final, p_url),
                        timeout=45.0
                    )
                    if exito:
                        logger.info(f"Suscripción completada con éxito usando {cookie_id}")
                        break
                    else:
                        logger.warning(f"Fallo en intento de {cookie_id} con proxy {p_url}. Pasando a reintento...")
                except asyncio.TimeoutError:
                    logger.error(f"⏱️ TIMEOUT CRÍTICO: La cuenta {cookie_id} tardó más de 45s con proxy {p_url}. Cancelando intento.")
                except Exception as e:
                    logger.error(f"Error inesperado en hilo de ejecución para {cookie_id}: {e}")
                
                # Pausa antes de que la misma cuenta intente con otra IP
                await asyncio.sleep(random.randint(2, 4))
            
            if not exito:
                # Si falló definitivamente en todos los intentos de proxy, se marca como muerta
                await marcar_cookie_estado(cookie_id, "dead")
                mover_cookie_caducada(cookie_id)
            
            await monitor.registrar_resultado(exito)

    # Disparamos todas las tareas concurrentes de forma controlada asíncronamente
    tareas = [asyncio.create_task(tarea_worker(cid)) for cid in cookies_a_usar]
    await asyncio.gather(*tareas)

    # Informe de finalización
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ **Campaña Finalizada**\n\n├ Objetivo: {url_final}\n├ Procesados: {cantidad_real}\n├ ✅ Éxitos: {monitor.exitos}\n└ ❌ Fallidos: {monitor.fallidos}"
    )

# ==========================================
# FUNCIONES AUXILIARES: TESTEO Y REPORTES
# ==========================================
async def analizar_estado_cookies(message):
    """Chequeo de cookies activas rápido."""
    cookies_archivos = list(COOKIES_DIR.glob("*.txt"))
    if not cookies_archivos:
        await message.reply_text("❌ No hay cookies cargadas en la carpeta `/cookies`.")
        return

    # Verificación de que no estén corruptas leyendo el formato básico
    validas = 0
    corruptas = 0
    for file in cookies_archivos:
        datos = parsear_netscape_cookies(file)
        if len(datos) > 0:
            validas += 1
        else:
            corruptas += 1
            mover_cookie_caducada(file.name)

    await message.reply_text(
        f"📊 **Análisis de Cookies de Cuentas:**\n\n"
        f"├ Cookies analizadas: `{len(cookies_archivos)}`\n"
        f"├ Estructura válida: `{validas}`\n"
        f"└ Corruptas / Movidas: `{corruptas}`"
    )

async def testear_lista_proxies(message):
    """Mide la latencia de las proxies frente a los servidores de TikTok."""
    proxies = obtener_lista_proxies()
    if not proxies:
        await message.reply_text("❌ El archivo `proxies.txt` está vacío.")
        return

    testeadas = 0
    activas = 0
    for p in proxies[:15]: # Testeamos solo una muestra rápida de 15 para no demorar la respuesta de Telegram
        testeadas += 1
        try:
            async with httpx.AsyncClient(proxies={"all://": p}, timeout=5.0) as client:
                resp = await client.get("https://www.tiktok.com/")
                if resp.status_code == 200:
                    activas += 1
        except Exception:
            pass

    await message.reply_text(
        f"📡 **Test de Muestra de Proxies:**\n\n"
        f"├ Total en `proxies.txt`: `{len(proxies)}`\n"
        f"├ Muestra testeada: `{testeadas}`\n"
        f"└ Estado funcional de la muestra: `{activas} / {testeadas}`"
    )

async def generar_reporte_stats(message):
    """Consulta la base de datos de SQLite y compila estadísticas de uso real."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*), status FROM cookies GROUP BY status")
    filas_status = cursor.fetchall()
    
    cursor.execute("SELECT COUNT(*) FROM tracking_seguimiento")
    total_follows = cursor.fetchone()[0]
    
    conn.close()

    status_dict = {"active": 0, "dead": 0, "captcha": 0}
    for count, status in filas_status:
        if status in status_dict:
            status_dict[status] = count

    texto_reporte = (
        f"📈 **Reporte de Rendimiento del Panel SMM**\n\n"
        f"├ Total de Cuentas Sólidas: `{status_dict['active']}`\n"
        f"├ Cuentas Muertas: `{status_dict['dead']}`\n"
        f"├ Cuentas Bloqueadas temporalmente (Captcha): `{status_dict['captcha']}`\n"
        f"└ Total de Follows Entregados: `{total_follows}`\n\n"
        f"⚡ _Recuerda refrescar tus cookies caducadas de vez en cuando._"
    )
    await message.reply_text(texto_reporte, parse_mode="Markdown")

# ==========================================
# INICIALIZACIÓN PRINCIPAL
# ==========================================
def main():
    # Inicializamos base de datos
    inicializar_db()

    # Arrancamos servidor HTTP interno para el Health Check de Render
    iniciar_servidor_ping()

    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        logger.error("❌ ERROR CRÍTICO: No se ha detectado la variable de entorno TELEGRAM_TOKEN.")
        sys.exit(1)

    logger.info("🤖 Iniciando aplicación del Bot de Telegram...")
    application = Application.builder().token(token).build()

    # ConversationHandler blindado para evitar el bucle infinito al meter el número
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("follow", callback_menu),
            CallbackQueryHandler(callback_menu, pattern="^btn_iniciar$")
        ],
        states={
            AWAITING_QUANTITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_cantidad)
            ],
            AWAITING_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_enlace)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", comando_cancel),
            MessageHandler(filters.COMMAND, comando_cancel) # Si mete cualquier comando durante el flujo, cancelamos de inmediato.
        ],
        allow_reentry=True
    )

    # Registro de Handlers ordinarios
    application.add_handler(CommandHandler("start", comando_start))
    application.add_handler(CallbackQueryHandler(callback_menu))
    application.add_handler(conv_handler)

    # Lanzamos el bot en modo Polling de escucha continua
    application.run_polling()

if __name__ == "__main__":
    main()
