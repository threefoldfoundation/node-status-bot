# Use official Python image
FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot files
COPY node-status-bot.py .
COPY db.py .
COPY find_violations.py .
COPY ingester.py .

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Run the bot
CMD ["python", "node-status-bot.py"]
