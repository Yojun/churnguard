FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m app && chown -R app /app
USER app
# 公開部署預設值；雲端平台通常在 HTTPS 反向代理之後，所以開 TRUST_PROXY 與 HTTPS
ENV CHURNGUARD_PUBLIC=1 CHURNGUARD_TRUST_PROXY=1 CHURNGUARD_HTTPS=1 PORT=8000
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz')"
CMD ["gunicorn", "-c", "gunicorn.conf.py", "churnguard.web:app"]
