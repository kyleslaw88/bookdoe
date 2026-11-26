"""Containers run non-root, images are pulled by default and published multi-arch."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_dockerfiles_end_as_non_root_user():
    for name, user in (("bookdoe", "bookdoe"), ("openreads", "openreads")):
        lines = (ROOT / name / "Dockerfile").read_text().splitlines()
        users = [l.split()[1] for l in lines if l.startswith("USER ")]
        assert users and users[-1] == user


def test_android_ci_dockerfile_has_user():
    lines = (ROOT / "android" / "ci" / "Dockerfile").read_text().splitlines()
    users = [l.split()[1] for l in lines if l.startswith("USER ")]
    assert users and users[-1] not in ("root", "0")


def test_compose_pulls_published_images_keeps_build_and_sets_user():
    text = (ROOT / "docker-compose.yml").read_text()
    assert "ghcr.io/<owner>/bookdoe:latest" in text and "ghcr.io/ada/openreads:latest" in text
    assert "build: ./openreads" in text and "context: ./bookdoe" in text
    assert text.count('user: "${PUID:-1000}:${PGID:-1000}"') == 2


def test_release_publishes_both_images_for_amd64_and_arm64():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "linux/amd64,linux/arm64" in text and "setup-qemu-action" in text
    assert "name: openreads" in text and "matrix.name" in text
    assert "type=semver,pattern={{version}}" in text


def test_install_pulls_before_starting():
    text = (ROOT / "install.sh").read_text()
    assert "docker compose pull" in text and "docker compose up -d\n" in text
