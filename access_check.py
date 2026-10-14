"""Проверка: видит ли сервисный аккаунт бота файлы Нины. Пишет state/access_check.json (ok / ошибка / вкладки / шапка)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import loaders

FILES = {
    "Stock UK-DE": "1XKUoiKUa8aF0DT7pNgXRqEvlejVu_FYVpMRPyGcOlLI",
    "Stock Amazon Merino": "1acpHlfBiJjFm96VmFksRmKTnktULTgmu7iESYFDLUZw",
    "Sales plan +Reorder US": "1b3RWGwKP3_g_bA8JCuc6-XHCTRw3KiPlW8ROIUFH9qs",
    "Sales plan +Reorder CA": "1rJH8IJv9gg1nhsbDuN103xAAxkoLmmie915vaCEwgTQ",
    "Sales plan +Reorder DE": "1mfvv2zIq7YlnsbWfHtwtajR2Ic4RCwChPODbLxqIiAE",
    "Sales plan +Reorder UK": "1Mv1a0sxy_TqaPHxgB1_rMu9EUQK03oCzAu8GuBg0M7g",
    "Plan Sales 2025 CA": "1v5QvwvchtYC0OnEGAzHZdlkf6Hmd6GHbp0_9NcxrUj0",
    "Stock CA": "1FRP8eWrJRNraSeua1kJn_rki9H4XshUdiKRMw9w8QnA",
    "Reorder for summer'27": "1hNfTvZedPygVhlRuJhKfBKujcEgh0eQbxTpj_l8mLcY",
}
PROBE = {"Stock Amazon Merino": "Orders", "Stock UK-DE": "Order DE", "Sales plan +Reorder US": "Margin"}


def main() -> int:
    out = {}
    for name, fid in FILES.items():
        try:
            book = loaders._gspread_book("sheet:" + fid)
            tabs = [w.title for w in book.worksheets()]
            res = {"ok": True, "tabs": len(tabs), "tab_names": tabs[:60]}
            probe = PROBE.get(name)
            if probe:
                ws = next((w for w in book.worksheets() if loaders._norm_tab(w.title) == loaders._norm_tab(probe)), None)
                res["probe"] = {"tab": probe, "found": ws is not None}
                if ws is not None:
                    res["probe"]["header"] = [str(c) for c in ws.get("A1:AZ1")[0]][:52]
            out[name] = res
        except Exception as exc:  # noqa: BLE001
            out[name] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    p = Path("state/access_check.json")
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v.get("ok") for k, v in out.items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
