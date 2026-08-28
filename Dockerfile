FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
# UVICORN_HOST matters under network_mode: host, where there is no port
# mapping to hide behind: 0.0.0.0 would expose the dashboard on the EC2
# public interface. The host-mode compose stack sets it to 127.0.0.1.
CMD ["sh", "-c", "uvicorn app:app --host ${UVICORN_HOST:-0.0.0.0} --port ${PORT:-8000}"]