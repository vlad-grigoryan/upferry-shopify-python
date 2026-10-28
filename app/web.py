import os

import uvicorn
from dotenv import load_dotenv

if __name__ == "__main__":
    load_dotenv()
    # Access logs include query strings. Shopify page requests may carry tokens.
    uvicorn.run("app.main:create_app", factory=True, host="0.0.0.0", port=int(os.getenv("PORT", "8000")), access_log=False, proxy_headers=True, forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"))
