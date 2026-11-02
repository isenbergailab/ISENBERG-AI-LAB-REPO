FROM python:3.12-slim
WORKDIR /app
COPY . .
ENV PYTHONDONTWRITEBYTECODE=1
USER 65534:65534
CMD ["python", "-B", "-m", "unittest", "discover", "-s", "tests", "-v"]
