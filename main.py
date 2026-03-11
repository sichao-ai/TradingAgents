import os

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Support OpenAI-compatible providers (for example MiniMax) with their own key name.
if not os.getenv("OPENAI_API_KEY") and os.getenv("MINIMAX_API_KEY"):
    os.environ["OPENAI_API_KEY"] = os.getenv("MINIMAX_API_KEY", "")

# Create a custom config
config = DEFAULT_CONFIG.copy()
default_model = os.getenv("TA_MODEL", "gpt-5-mini")
config["llm_provider"] = os.getenv("TA_LLM_PROVIDER", config["llm_provider"])
config["backend_url"] = os.getenv(
    "MINIMAX_BASE_URL", os.getenv("TA_BACKEND_URL", config["backend_url"])
)
config["deep_think_llm"] = os.getenv("TA_DEEP_MODEL", default_model)
config["quick_think_llm"] = os.getenv("TA_QUICK_MODEL", default_model)
config["max_debate_rounds"] = int(os.getenv("TA_MAX_DEBATE_ROUNDS", "1"))

# Configure data vendors (default uses yfinance, no extra API keys needed)
config["data_vendors"] = {
    "core_stock_apis": "alpha_vantage",      # Options: local, alpha_vantage, yfinance
    "technical_indicators": "alpha_vantage", # Options: local, alpha_vantage, yfinance
    "fundamental_data": "alpha_vantage",     # Options: alpha_vantage, yfinance
    "news_data": "alpha_vantage",            # Options: alpha_vantage, yfinance
}

# Initialize with custom config
# Set debug=False to avoid dumping long intermediate reports to terminal.
ta = TradingAgentsGraph(debug=False, config=config)

# forward propagate
_, decision = ta.propagate("NVDA", "2024-05-10")
print(decision)

# Memorize mistakes and reflect
# ta.reflect_and_remember(1000) # parameter is the position returns
