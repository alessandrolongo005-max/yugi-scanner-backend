FROM python:3.11-slim
WORKDIR /app
# Installa le dipendenze per OpenCV
RUN apt-get update && apt-get install -y libgl1 libglib2.0-0
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
# Avvia il server
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
