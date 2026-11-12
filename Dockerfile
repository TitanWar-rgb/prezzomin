FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

# Crea l'utente con UID 1000 per compatibilità nativa con Hugging Face Spaces
RUN useradd -m -u 1000 user

WORKDIR /app

# Installa le dipendenze Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia i file del progetto
COPY . .

# Assegna i permessi della cartella all'utente
RUN chown -R user:user /app

USER user

# Porta richiesta da Hugging Face Spaces
ENV PORT=7860
EXPOSE 7860

# Avvia l'applicazione con dashboard e scraper
CMD ["python", "app.py"]
