# The keeper: a Claude Code agent outside the VM that repairs a machine which cannot recover
# itself (KEEPER.md). keeper.py runs from the mounted project, so a fix to it lands with the
# repository and needs no rebuild; this image holds only the tools a repair uses.
# The machine's own suite runs on CPython 3.14, so the keeper's targeted tests do too.
FROM python:3.14-slim-trixie

ARG TZ
ENV TZ="$TZ"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg2 wget git gh jq openssh-client procps less \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

# Node.js for Claude Code
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
  && apt-get install -y --no-install-recommends nodejs \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

# The .NET 8 SDK, which compiles the launcher with EnableWindowsTargeting before a push
RUN wget -q https://packages.microsoft.com/config/debian/13/packages-microsoft-prod.deb -O /tmp/ms.deb \
  && dpkg -i /tmp/ms.deb && rm /tmp/ms.deb \
  && apt-get update && apt-get install -y --no-install-recommends dotnet-sdk-8.0 \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

# PowerShell 7 from its release tarball, which parses every .ps1 before a push
# (docs/driving-the-vm.md § Checking what only Windows runs)
ARG PWSH_VERSION=7.6.6
RUN mkdir -p /opt/pwsh \
  && curl -fsSL "https://github.com/PowerShell/PowerShell/releases/download/v${PWSH_VERSION}/powershell-${PWSH_VERSION}-linux-x64.tar.gz" \
     | tar -xz -C /opt/pwsh \
  && chmod +x /opt/pwsh/pwsh && ln -s /opt/pwsh/pwsh /usr/local/bin/pwsh

# paramiko reaches the VM; the rest is what SESSION-START's targeted tests import
RUN pip install --no-cache-dir paramiko pytest pytest-asyncio pytest-timeout docker mcp==2.2.0 \
    watchfiles brotli pyyaml

RUN npm install -g @anthropic-ai/claude-code

# The project is a bind mount from a Windows drive, owned by nobody git recognises; and every
# commit on the public repository carries the owner's noreply identity.
RUN git config --system --add safe.directory '*' \
  && git config --system user.email 138273879+ChristianBlevens@users.noreply.github.com \
  && git config --system user.name "Christian Blevens"

# Claude Code refuses --dangerously-skip-permissions as root outside a sandbox; this container
# is the sandbox.
ENV IS_SANDBOX=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /work/RaiGolmi
ENTRYPOINT ["python3", "/work/RaiGolmi/keeper/keeper.py"]
