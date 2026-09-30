import os

_SVG_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     width="1920" height="1080"
     viewBox="0 0 1920 1080">
  <text
     x="957.21832"
     y="1013.0276"
     style="font-family:'Ubuntu';font-weight:bold;font-size:56px;fill:#ffef0e;text-anchor:middle;font-style:normal;"
     >{text}</text>
</svg>
"""


def generate_score_svg(near: int, far: int, output_path: str) -> None:
    """Write a score overlay SVG file with the given Near/Far scores."""
    content = _SVG_TEMPLATE.format(text=f"Near: {near} | Far: {far}")
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(content)
