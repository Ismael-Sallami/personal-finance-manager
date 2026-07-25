"""Shared Jinja2 instance with the template filters used across the app."""
from fastapi.templating import Jinja2Templates

from app.config import APP_ROOT
from app.format import money, pct

templates = Jinja2Templates(directory=str(APP_ROOT / "app" / "templates"))

MONTHS = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def month_name(m: int) -> str:
    return MONTHS[m] if 1 <= int(m) <= 12 else str(m)


templates.env.filters["money"] = money
templates.env.filters["month"] = month_name
templates.env.filters["pct"] = pct
templates.env.globals["MONTHS"] = MONTHS


# --- TemplateResponse signature shim ---
# Starlette >=1.0 wants TemplateResponse(request, name, context). The views here
# use the older TemplateResponse(name, {"request": request, ...}). This accepts
# both so the call sites stay readable.
_orig_template_response = templates.TemplateResponse


def _template_response_compat(*args, **kwargs):
    if args and isinstance(args[0], str):
        name = args[0]
        context = args[1] if len(args) > 1 else kwargs.pop("context", {}) or {}
        request = context.get("request")
        return _orig_template_response(request, name, context, *args[2:], **kwargs)
    return _orig_template_response(*args, **kwargs)


templates.TemplateResponse = _template_response_compat
