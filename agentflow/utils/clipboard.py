def copy_text(text: str) -> bool:
    """Copy to clipboard via pyperclip if available; else False."""
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        return False
