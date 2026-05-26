FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser
RUN playwright install chromium

# Copy all scrapers and orchestrator
COPY main.py .
COPY unified_Scrapper.py .
COPY AP_scrapper.py .

CMD ["python", "main.py"]
