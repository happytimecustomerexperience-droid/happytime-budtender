"""Order confirmation email.

The success page is the only confirmation a shopper gets otherwise — close the tab
and all they have is a name and a store. So when they give us an email, we send the
order back to them.

Never blocks or breaks checkout. Sending is best-effort: a dead SMTP server must
not lose an order that is already saved and already visible to staff.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils.html import escape

logger = logging.getLogger(__name__)


def _from_addr() -> str:
    return (getattr(settings, "DEFAULT_FROM_EMAIL", "") or "").strip()


def enabled() -> bool:
    """Only send when a real backend and a from-address are configured."""
    backend = getattr(settings, "EMAIL_BACKEND", "")
    if "dummy" in backend:
        return False
    return bool(_from_addr())


def _lines_text(lines: list[dict]) -> str:
    out = []
    for line in lines or []:
        if not line.get("in_stock", True):
            continue
        name = line.get("name", "")
        brand = line.get("brand", "")
        qty = line.get("quantity", 1)
        total = line.get("line_total", 0)
        suffix = f" ({brand})" if brand else ""
        out.append(f"  {qty} x {name}{suffix} — ${total:.2f}")
    return "\n".join(out)


def _lines_html(lines: list[dict]) -> str:
    rows = []
    for line in lines or []:
        if not line.get("in_stock", True):
            continue
        rows.append(
            "<tr>"
            f"<td style='padding:6px 10px 6px 0'>{escape(str(line.get('quantity', 1)))}&times;</td>"
            f"<td style='padding:6px 10px 6px 0'>{escape(line.get('name', ''))}"
            f"<br><span style='color:#5d6b57;font-size:12px'>{escape(line.get('brand', ''))}</span></td>"
            f"<td style='padding:6px 0;text-align:right;white-space:nowrap'>"
            f"${float(line.get('line_total', 0)):.2f}</td>"
            "</tr>"
        )
    return "".join(rows)


def send_order_confirmation(draft, store_label: str, store_address: str) -> bool:
    """Email the shopper their order. Returns True if handed to the backend."""
    to = (draft.contact_email or "").strip()
    if not to or not enabled():
        return False

    quote = draft.quote or {}
    lines = draft.lines or []
    total = float(quote.get("total") or 0)
    bundle = quote.get("bundle_name")
    pct = quote.get("bundle_discount_pct")
    code = (draft.draft_token or "")[-6:].upper()
    # Built by hand rather than strftime: "%-I" is glibc-only and "%#I" is Windows
    # -only, so either one breaks on the other platform.
    held = ""
    if draft.expires_at:
        t = draft.expires_at
        hour = t.hour % 12 or 12
        held = f"{hour}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'} on {t.strftime('%A')}"

    bundle_line = ""
    if bundle and pct:
        bundle_line = (f"\nMention your {bundle} at the counter so your "
                       f"{pct}% discount is applied.\n")

    text = f"""Hi {draft.pickup_name},

Your order is being held at {store_label}.

{_lines_text(lines)}

Subtotal: ${total:.2f}
Taxes and the final total are calculated at the register.
{bundle_line}
Pickup name: {draft.pickup_name}
Order code:  {code}
Address:     {store_address}
{f"Held until:  {held}" if held else ""}

Just walk in and give your name at the counter. Bring valid ID — you must be 21+.
Payment happens in store; nothing has been charged.

— {store_label}
"""

    html = f"""<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
color:#14210f;max-width:520px">
<h2 style="margin:0 0 4px">Your order is being held</h2>
<p style="margin:0 0 16px;color:#5d6b57">{escape(store_label)} · {escape(store_address)}</p>
<table style="width:100%;border-collapse:collapse;font-size:14px">{_lines_html(lines)}</table>
<p style="border-top:1px solid #e2e7de;margin-top:12px;padding-top:10px;font-weight:700">
Subtotal: ${total:.2f}</p>
<p style="font-size:12px;color:#5d6b57;margin:0 0 16px">
Taxes and the final total are calculated at the register.</p>
{f'<p style="background:#fff6e5;color:#7a4b00;padding:10px 12px;border-radius:8px">Mention your <strong>{escape(bundle)}</strong> at the counter so your {escape(str(pct))}% discount is applied.</p>' if bundle and pct else ''}
<table style="font-size:14px;margin:12px 0">
<tr><td style="color:#5d6b57;padding-right:14px">Pickup name</td><td><strong>{escape(draft.pickup_name)}</strong></td></tr>
<tr><td style="color:#5d6b57;padding-right:14px">Order code</td><td><strong style="letter-spacing:.12em">{escape(code)}</strong></td></tr>
{f'<tr><td style="color:#5d6b57;padding-right:14px">Held until</td><td><strong>{escape(held)}</strong></td></tr>' if held else ''}
</table>
<p style="font-size:13px;color:#5d6b57">Walk in and give your name at the counter. Bring valid
ID — you must be 21+. Payment happens in store; nothing has been charged.</p>
</div>"""

    try:
        msg = EmailMultiAlternatives(
            subject=f"Your order at {store_label} — code {code}",
            body=text, from_email=_from_addr(), to=[to],
        )
        msg.attach_alternative(html, "text/html")
        msg.send(fail_silently=False)
        return True
    except Exception:
        # The order is already saved and already in the staff queue; a mail failure
        # must never surface to the shopper as a failed checkout.
        logger.warning("order confirmation email failed for draft %s",
                       draft.draft_token, exc_info=True)
        return False
