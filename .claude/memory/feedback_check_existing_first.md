---
name: feedback-check-existing-first
description: Always check for existing implementations (libraries, patterns, utilities) before proposing custom code
metadata:
  node_type: memory
  type: feedback
  originSessionId: current
---

Перед тем как предлагать свою реализацию — сначала искать готовые решения: в кодовой базе проекта, в PyPI, в стандартной библиотеке.

**Why:** пользователь явно недоволен тем, что новые реализации предлагаются без проверки существующих.

**How to apply:** для каждого нового модуля или функции — сначала grep по репозиторию на похожие паттерны, затем проверить есть ли PyPI-пакет (особенно специализированный). Только если ничего не найдено — предлагать своё. Явно указывать в плане: "используем существующий X из файла Y" или "используем библиотеку Z".
