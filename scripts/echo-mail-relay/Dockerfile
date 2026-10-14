FROM python:3.12-slim
RUN useradd --uid 10001 --create-home relay && mkdir /data && chown relay:relay /data
WORKDIR /app
COPY scripts/echo-mail-relay/relay.py /app/relay.py
COPY scripts/echo-mail-relay/test_smtp.py /app/test_smtp.py

USER relay
EXPOSE 8080
VOLUME /data
CMD ["python", "/app/relay.py"]
