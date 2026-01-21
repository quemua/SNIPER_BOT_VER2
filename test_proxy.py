import requests

proxy = "socks5h://6nL8BpRvvASxVC:vxoEs1OwzZjYOyq@66.245.167.148:45497"

proxies = {
    "http": proxy,
    "https": proxy
}

try:
    r = requests.get("https://polymarket.com", proxies=proxies, timeout=15)
    print(f"✅ Status: {r.status_code}")
except Exception as e:
    print(f"❌ Lỗi: {e}")