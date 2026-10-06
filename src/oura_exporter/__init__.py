def __getattr__(name: str) -> str:
    # Resolved on first use: importlib.metadata is slow to import and --healthcheck never needs it.
    if name != "__version__":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib.metadata import PackageNotFoundError, version

    try:
        resolved = version("oura-exporter")
    except PackageNotFoundError:
        resolved = "0.0.0"
    globals()[name] = resolved
    return resolved
