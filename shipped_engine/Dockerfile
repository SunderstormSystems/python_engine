# -------- base image ----------
    FROM python:3.11-slim AS base

    # -- System prep (no compiler needed) --
    ENV PYTHONDONTWRITEBYTECODE=1 \
        PYTHONUNBUFFERED=1 \
        PIP_NO_CACHE_DIR=1
    
    WORKDIR /app
    
    # -------- dependencies layer ----------
    COPY requirements.txt .
    RUN pip install -r requirements.txt
    
    # -------- application layer ----------
    COPY app/ app/
    COPY .streamlit/ .streamlit/
    
    # Expose Streamlit’s default port
    EXPOSE 8501
    
    # Entrypoint
    CMD ["streamlit", "run", "app/shipped_engine_v1.py"]