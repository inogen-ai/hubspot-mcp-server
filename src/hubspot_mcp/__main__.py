"""`python -m hubspot_mcp` entry point — delegates to the same `main()` the console
script (`hubspot-mcp-server`) uses, so both invocation styles behave identically."""

from hubspot_mcp.server import main

if __name__ == "__main__":
    main()
