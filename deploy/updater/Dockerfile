# FDPneo host updater agent (ADR-0030). Optional: started only with the
# `updater` compose profile. It talks to the Docker daemon through the mounted
# socket, which is root-equivalent on a standard Docker host.
FROM docker:29.9-cli
RUN apk add --no-cache python3
COPY fdpneo_updater.py /usr/local/bin/fdpneo-updater
RUN chmod 0755 /usr/local/bin/fdpneo-updater
ENTRYPOINT ["python3", "/usr/local/bin/fdpneo-updater"]
