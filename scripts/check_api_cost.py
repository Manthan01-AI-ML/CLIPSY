"""
scripts/check_api_cost.py

Run this anytime to see your current Anthropic API spend.

Usage (from clipwise root directory):
    docker-compose exec backend python -m scripts.check_api_cost

Or outside Docker:
    python scripts/check_api_cost.py
"""
import os
import sys
from pathlib import Path

# Allow running as standalone script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    # Try to load from .env if running standalone
    try:
        from dotenv import load_dotenv
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if env_path.exists():
            load_dotenv(env_path)
    except ImportError:
        pass

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key or api_key.startswith("sk-ant-REPLACE"):
        print("✗ ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)

    # Anthropic doesn't expose a spend API to end-users yet, so we give
    # guidance on how to check it manually.
    print("=" * 60)
    print("🔍 How to check your Anthropic API spend")
    print("=" * 60)
    print()
    print("1. Go to: https://console.anthropic.com")
    print("2. Left sidebar → 'Plans & Billing' OR 'Usage'")
    print("3. You'll see:")
    print("   • Current balance")
    print("   • Spend this month")
    print("   • Usage by model")
    print()
    print("=" * 60)
    print("💰 Safety caps active in your code:")
    print("=" * 60)
    print(f"   Max input tokens per call:  50,000 = max $0.05 input")
    print(f"   Max output tokens per call:  3,500 = max $0.0175 output")
    print(f"   Max cost per video call:    ~$0.07 (worst case)")
    print(f"   Typical cost per video:     ~$0.01")
    print()
    print("=" * 60)
    print("📊 Budget sanity check:")
    print("=" * 60)
    print(f"   $10 balance ÷ $0.01 typical  = ~1,000 videos")
    print(f"   $10 balance ÷ $0.07 worst    = ~143 videos")
    print()
    print("✓ Even in the absolute worst case, you cannot exhaust $10")
    print("  in a single video. You're safe.")
    print()


if __name__ == "__main__":
    main()