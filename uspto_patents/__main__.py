"""Entry point: python -m uspto_patents"""
import asyncio

from .main import main

if __name__ == "__main__":
    asyncio.run(main())
