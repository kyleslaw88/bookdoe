# Build image for the Android app: JDK plus Android SDK 35 (platform and
# build-tools). Used by android/ci/build.sh for a local, emulator-free run of the
# same commands the GitHub workflow executes.
#
# On an arm64 host the Android build tools (aapt2) are still x86_64 binaries, so
# the image adds the amd64 C runtime and relies on the host having amd64 binfmt
# emulation registered (docker run --privileged --rm tonistiigi/binfmt --install
# amd64). Only aapt2 runs emulated; Gradle, Kotlin and the tests stay native.
FROM ghcr.io/cirruslabs/android-sdk:35
ENV GRADLE_OPTS="-Dorg.gradle.daemon=false"
RUN set -eu; \
    if [ "$(uname -m)" = "aarch64" ]; then \
      sed -i 's/^Types: deb$/Types: deb\nArchitectures: arm64/' /etc/apt/sources.list.d/ubuntu.sources; \
      dpkg --add-architecture amd64; \
      printf 'Types: deb\nURIs: http://archive.ubuntu.com/ubuntu\nSuites: noble noble-updates\nComponents: main universe\nArchitectures: amd64\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' \
        > /etc/apt/sources.list.d/amd64.sources; \
      apt-get update; \
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends libc6:amd64 libstdc++6:amd64 zlib1g:amd64; \
      rm -rf /var/lib/apt/lists/*; \
    fi
WORKDIR /workspace
# Unprivileged by default (trivy DS-0002). build.sh overrides this with --user <host uid>:<gid>.
RUN useradd --create-home --uid 1000 ci || true
USER 1000
