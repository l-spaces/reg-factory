# -*- coding: utf-8 -*-
"""
验证 extract_graph_tokens.py 的数据库保存逻辑
"""

import sys
from common.store import get_store

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# 模拟一个成功提取的结果
mock_result = {
    'email': 'test_extract@outlook.com',
    'password': 'test_password_123',
    'refresh_token': 'mock_refresh_token_xyz',
    'client_id': '9e5f94bc-e8a4-4e73-b8be-63364c29d753'
}

print("Testing database save logic...")
print(f"Mock result: {mock_result['email']}")

store = get_store()

# 保存到数据库
try:
    store.add_email(
        email=mock_result['email'],
        password=mock_result['password'],
        refresh_token=mock_result.get('refresh_token', ''),
        client_id=mock_result.get('client_id', ''),
        source='extract_graph'
    )
    print("✓ Successfully saved to database")
except Exception as e:
    print(f"✗ Failed to save: {e}")
    sys.exit(1)

# 验证数据是否正确保存
emails = store.list_emails()
found = False
for email_record in emails:
    if email_record['email'] == mock_result['email'].lower():
        found = True
        print(f"\n✓ Email found in database:")
        print(f"  email: {email_record['email']}")
        print(f"  password: {email_record['password']}")
        print(f"  refresh_token: {email_record['refresh_token'][:30]}...")
        print(f"  client_id: {email_record['client_id']}")
        print(f"  source: {email_record['source']}")

        # 验证字段
        assert email_record['password'] == mock_result['password'], "Password mismatch"
        assert email_record['refresh_token'] == mock_result['refresh_token'], "Refresh token mismatch"
        assert email_record['client_id'] == mock_result['client_id'], "Client ID mismatch"
        assert email_record['source'] == 'extract_graph', "Source mismatch"
        print("\n✓ All fields verified successfully")
        break

if not found:
    print("✗ Email not found in database")
    sys.exit(1)

# 测试幂等性 - 重复保存应该不会出错
print("\nTesting idempotency (duplicate save)...")
try:
    store.add_email(
        email=mock_result['email'],
        password=mock_result['password'],
        refresh_token=mock_result.get('refresh_token', ''),
        client_id=mock_result.get('client_id', ''),
        source='extract_graph'
    )
    print("✓ Duplicate save handled correctly (idempotent)")
except Exception as e:
    print(f"✗ Duplicate save failed: {e}")
    sys.exit(1)

print("\n" + "=" * 60)
print("All tests passed! ✓")
print("=" * 60)
