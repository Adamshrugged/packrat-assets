def toml_str(path) -> str:
    """A path as a TOML literal string: single quotes take Windows backslashes as they are."""
    return f"'{path}'"
