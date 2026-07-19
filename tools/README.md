# Portable tools

## gemini_image.py — text-to-image via Gemini

A standalone, dependency-light image generator (Google Gen AI SDK), generalized
from the podforge renderer for reuse anywhere.

Install: `pip install -r requirements.txt`

Key: set `GEMINI_API_KEY` in your environment, or pass `--env-file path/to/.env`.

CLI:
```
python gemini_image.py "a serene crystal city at dusk" -o out.png --aspect 16:9
python gemini_image.py "portrait, same style" -o p.png --ref moodboard.png
```

Library:
```python
from gemini_image import render_image, save_image
save_image("prompt", "out.png", aspect="3:4", seed=7)
data = render_image("prompt", reference_images=["ref.png"])  # PNG bytes
```

Options: `--aspect` (1:1, 16:9, 3:4, …), `--model` (default gemini-2.5-flash-image),
`--seed`, `--ref` (repeatable reference images), `--env-file`.
