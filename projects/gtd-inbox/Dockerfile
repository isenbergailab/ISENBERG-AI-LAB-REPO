FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY gtd_agent/ ./gtd_agent/
COPY examples/ ./examples/
COPY tools/demo.py ./tools/demo.py
USER 65534:65534
CMD ["python", "-B", "tools/demo.py"]
