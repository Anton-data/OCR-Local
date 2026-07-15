# MinerU development image for NVIDIA GPUs (Volta through Blackwell).
FROM vllm/vllm-openai:v0.21.0

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        fontconfig \
        fonts-noto-cjk \
        fonts-noto-core \
        libgl1 && \
    fc-cache -fv && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Headless LibreOffice Writer for local DOCX -> PDF preview conversion in the
# Gradio UI (no GUI packages); fonts-liberation adds Cyrillic-capable fonts.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libreoffice-writer-nogui \
        fonts-liberation && \
    fc-cache -fv && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /workspace/MinerU

COPY . /workspace/MinerU

# Install the checked-out source in editable mode. The vLLM base image already
# provides the GPU inference engine used by MinerU on Linux.
RUN python3 -m pip install --break-system-packages -e ".[core]" && \
    python3 -m pip cache purge

# Pin pdftext to 0.6.3 for compatibility with pdftext.pdf.chars API
# (0.7.1 introduces PageChars without __iter__, breaking pipeline/hybrid backends)
RUN python3 -m pip install --break-system-packages "pdftext==0.6.3" && \
    python3 -m pip cache purge

ENTRYPOINT ["/bin/bash", "-c", "exec \"$@\"", "--"]
CMD ["mineru-api", "--host", "0.0.0.0", "--port", "8000"]

