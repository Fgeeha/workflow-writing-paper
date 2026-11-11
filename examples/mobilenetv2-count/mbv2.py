"""Пример сценария эксперимента: параметры и умножения-сложения MobileNetV2 (ширина 1.0, 224×224, 1000 классов).

Считается по таблице блоков из статьи Sandler et al., 2018; результат — JSON для `mapping` сценария.
Запуск: python3 mbv2.py out/m.json → {"params_m": 3.505, "macs_m": 300.8}
"""

import json
import sys
from pathlib import Path

# (t — расширение, c — каналы на выходе, n — повторов, s — шаг первого повтора)
BLOCKS = [(1, 16, 1, 1), (6, 24, 2, 2), (6, 32, 3, 2), (6, 64, 4, 2), (6, 96, 3, 1), (6, 160, 3, 2), (6, 320, 1, 1)]


def conv(cin: int, cout: int, k: int, hw: int, groups: int = 1) -> tuple[int, int]:
    """Свёртка с батч-нормализацией: (параметры, умножения-сложения) на выходной карте hw×hw."""
    weights = cin // groups * cout * k * k
    return weights + 2 * cout, weights * hw * hw


def count() -> tuple[int, int]:
    params, macs = conv(3, 32, 3, 112)
    hw, cin = 112, 32
    for t, c, n, s in BLOCKS:
        for i in range(n):
            hidden, out_hw = cin * t, hw // (s if i == 0 else 1)
            layers = [conv(cin, hidden, 1, hw)] if t != 1 else []
            layers += [conv(hidden, hidden, 3, out_hw, groups=hidden), conv(hidden, c, 1, out_hw)]
            params += sum(p for p, _ in layers)
            macs += sum(m for _, m in layers)
            hw, cin = out_hw, c
    p, m = conv(cin, 1280, 1, hw)
    return params + p + 1280 * 1000 + 1000, macs + m + 1280 * 1000


if __name__ == "__main__":
    params, macs = count()
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "out/m.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"params_m": round(params / 1e6, 3), "macs_m": round(macs / 1e6, 1)}), encoding="utf-8")
