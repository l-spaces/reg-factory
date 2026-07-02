#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试 webui server 的 API 端点"""
import requests
import time

BASE_URL = "http://127.0.0.1:8799"

def test_delete_api():
    """测试 DELETE /api/accounts/{email}"""
    print("\n[TEST] DELETE API")

    # 先创建测试数据
    from common.store import get_store
    store = get_store()
    store.add_email("delete_test@example.com", "pass123")

    # 测试删除
    url = f"{BASE_URL}/api/accounts/delete_test@example.com"
    resp = requests.delete(url)
    print(f"  Status: {resp.status_code}")
    print(f"  Response: {resp.json()}")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    assert resp.json()["ok"] == True, "Expected ok=True"
    print("  [OK] DELETE API works")

def test_patch_api():
    """测试 PATCH /api/accounts/{email}"""
    print("\n[TEST] PATCH API")

    # 创建测试数据
    from common.store import get_store
    store = get_store()
    store.add_email("patch_test@example.com", "pass123")
    store.add_email("patch_exist@example.com", "pass456")

    # 测试更新密码
    url = f"{BASE_URL}/api/accounts/patch_test@example.com"
    resp = requests.patch(url, json={"password": "newpass123"})
    print(f"  Update password - Status: {resp.status_code}, Response: {resp.json()}")
    assert resp.status_code == 200 and resp.json()["ok"] == True

    # 测试更新邮箱
    resp = requests.patch(url, json={"new_email": "patch_test_new@example.com"})
    print(f"  Update email - Status: {resp.status_code}, Response: {resp.json()}")
    assert resp.status_code == 200 and resp.json()["ok"] == True

    # 测试唯一性冲突
    url = f"{BASE_URL}/api/accounts/patch_test_new@example.com"
    resp = requests.patch(url, json={"new_email": "patch_exist@example.com"})
    print(f"  Conflict test - Status: {resp.status_code}, Response: {resp.json()}")
    assert resp.status_code == 409, f"Expected 409, got {resp.status_code}"
    assert "已存在" in resp.json()["error"]

    print("  [OK] PATCH API works")

if __name__ == "__main__":
    print("Testing webui server APIs...")
    print("Make sure server is running on port 8799")

    # 等待服务器准备就绪
    for i in range(10):
        try:
            resp = requests.get(f"{BASE_URL}/api/status", timeout=1)
            if resp.status_code == 200:
                print("[OK] Server is ready")
                break
        except:
            time.sleep(1)
    else:
        print("[FAIL] Server not responding")
        exit(1)

    try:
        test_delete_api()
        test_patch_api()
        print("\n[OK] All API tests passed!")
    except Exception as e:
        print(f"\n[FAIL] Test failed: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
