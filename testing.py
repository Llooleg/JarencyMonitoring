# test_all_webhooks.py
import requests
import json
import time

BASE_URL = "http://localhost:8000/gitlab/webhook"

def test_webhook(payload_name, headers, payload):
    print(f"Testing {payload_name}...")
    try:
        response = requests.post(BASE_URL, headers=headers, json=payload, timeout=10)
        print(f"  Status: {response.status_code}")
        print(f"  Response: {response.json()}")
        return True
    except Exception as e:
        print(f"  Error: {e}")
        return False

# Push Event
push_headers = {"Content-Type": "application/json", "X-Gitlab-Event": "Push Hook"}
push_payload = {
    "object_kind": "push",
    "event_name": "push",
    "user_username": "jsmith",
    "user": {"name": "John Smith", "username": "jsmith", "email": "john@example.com"},
    "project": {"id": 123, "name": "test-project"},
    "commits": [{"id": "abc123", "message": "Test commit", "timestamp": "2023-01-01T12:00:00Z"}]
}

# Merge Request Event
mr_headers = {"Content-Type": "application/json", "X-Gitlab-Event": "Merge Request Hook"}
mr_payload = {
    "object_kind": "merge_request",
    "user": {"id": 1, "name": "Administrator", "username": "root", "email": "admin@example.com"},
    "project": {"id": 1, "name": "example-project"},
    "object_attributes": {
        "id": 99,
        "title": "Add new feature",
        "state": "opened",
        "url": "http://example.com/example-project/merge_requests/1"
    }
}

# Issue Event
issue_headers = {"Content-Type": "application/json", "X-Gitlab-Event": "Issue Hook"}
issue_payload = {
    "object_kind": "issue",
    "user": {"id": 1, "name": "Administrator", "username": "root", "email": "admin@example.com"},
    "project": {"id": 1, "name": "example-project"},
    "object_attributes": {
        "id": 301,
        "title": "Bug in login functionality",
        "state": "opened",
        "url": "http://example.com/example-project/issues/1"
    }
}

# Pipeline Event
pipeline_headers = {"Content-Type": "application/json", "X-Gitlab-Event": "Pipeline Hook"}
pipeline_payload = {
    "object_kind": "pipeline",
    "object_attributes": {
        "id": 31,
        "ref": "master",
        "status": "success",
        "duration": 300
    },
    "user": {"id": 1, "name": "Administrator", "username": "root", "email": "admin@example.com"},
    "project": {"id": 1, "name": "example-project"},
    "commit": {
        "id": "bcbb5ec396a2c0f828686f14fac9b80b780504f2",
        "message": "Update README.md",
        "author": {"name": "Administrator", "email": "admin@example.com"}
    }
}

# Test all webhooks
tests = [
    ("Push Event", push_headers, push_payload),
    ("Merge Request Event", mr_headers, mr_payload),
    ("Issue Event", issue_headers, issue_payload),
    ("Pipeline Event", pipeline_headers, pipeline_payload)
]

print("Starting webhook tests...")
print("=" * 50)

for name, headers, payload in tests:
    test_webhook(name, headers, payload)
    time.sleep(1)  # Brief pause between tests
    print("-" * 30)

print("All tests completed!")