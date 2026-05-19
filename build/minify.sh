#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
mkdir -p ui_dist

terser ui/app.js --compress --mangle --output ui_dist/app.js

html-minifier-terser ui/index.html \
  --collapse-whitespace --remove-comments \
  --minify-js true --minify-css true \
  --output ui_dist/index.html

echo "✓ ui_dist/ ready"
