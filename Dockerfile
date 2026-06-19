# Usamos la imagen oficial de Microsoft que ya viene con todo listo (Python y Chromium configurados)
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

# Creamos la carpeta de trabajo dentro del servidor
WORKDIR /app

# Copiamos e instalamos las dependencias de Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiamos todo el código de nuestro bot (incluyendo cookies y proxies)
COPY . .

# Comando para ejecutar nuestro bot directamente
CMD ["python", "bot.py"]
