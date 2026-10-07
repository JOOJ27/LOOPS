#!/bin/sh
# Duplo clique no Mac: o Finder abre este arquivo no Terminal automaticamente.
cd "$(dirname "$0")"
xattr -dr com.apple.quarantine ./loops 2>/dev/null || true
chmod +x ./loops
exec ./loops
