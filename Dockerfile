FROM python:3.11.15-slim-bookworm

# Copy uv binary from the official distroless image
COPY --from=ghcr.io/astral-sh/uv:0.9.24 /uv /uvx /usr/local/bin/

ARG USERNAME=vscode
ARG USER_UID=1000
ARG USER_GID=$USER_UID

# Create a non-root user with passwordless sudo
RUN apt-get update \
    && apt-get install -y --no-install-recommends sudo git ca-certificates \
    && groupadd --gid $USER_GID $USERNAME \
    && useradd --uid $USER_UID --gid $USER_GID -m -s /bin/bash $USERNAME \
    && echo "$USERNAME ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/$USERNAME \
    && chmod 0440 /etc/sudoers.d/$USERNAME \
    && rm -rf /var/lib/apt/lists/*

USER $USERNAME
WORKDIR /workspace

ENV UV_LINK_MODE=copy \
    PATH="/home/$USERNAME/.local/bin:$PATH"
