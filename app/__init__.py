

# 版本号的**唯一来源**：FastAPI 的 version、/api/health、以及 CHANGELOG 的最新条目都用它。
# 原来 app/main.py 里硬编码 `version='0.1.0'`，从 v0.1 一路到 v0.4.8 都没跟着走，
# /api/health 一直报 0.1.0 —— 升级后想确认「跑的是哪一版」时会被它骗。
# scripts/check_docs.py 会核对它与 CHANGELOG 最新版本号一致。
__version__ = '0.4.10'
