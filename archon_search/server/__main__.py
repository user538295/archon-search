"""Entry point: python -m archon_search.server"""
from archon_search.config import load_config
from archon_search.server.app import run_server

if __name__ == "__main__":
    run_server(load_config())
