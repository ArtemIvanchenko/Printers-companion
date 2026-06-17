---
name: feedback-no-hardcode-touch
description: Not to touch _CHAMBER_MAX_CM3 and M350 chamber comment in uploads.py
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 0489ce00-88e4-4bf6-872a-8fb29ca03cd2
---

`_CHAMBER_MAX_CM3 = 40_000` и комментарий `# M350 build chamber: 350 × 350 × 330 mm` в `api/routes/uploads.py` — **не трогать**.

**Why:** пользователь явно попросил не переносить это в MachineParams.

**How to apply:** при работе с uploads.py и MachineParams не переносить chamber size туда. Оставить как есть.
