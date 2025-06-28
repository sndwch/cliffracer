#!/usr/bin/env python3
"""
Send Cliffracer metrics to Zabbix
"""

import time

import requests


def send_to_zabbix():
    """Fetch metrics and display what would be sent to Zabbix"""
    try:
        # Get metrics from our exporter
        response = requests.get("http://localhost:9090/json")
        metrics = response.json()

        print("\n📊 Current Cliffracer Metrics (would send to Zabbix):")
        print("-" * 50)
        print(f"Orders Total: {metrics['orders_total']}")
        print(f"Orders/Minute: {metrics['orders_per_minute']}")
        print(f"Payment Success Rate: {metrics['payment_success_rate']}%")
        print(f"Avg Message Latency: {metrics['avg_message_latency_ms']}ms")
        print(f"Inventory Reserved: {metrics['inventory_items_reserved']} items")
        print(f"Notifications Sent: {metrics['notifications_sent_total']}")
        print(f"Avg Order Value: ${metrics['avg_order_value']}")
        print("-" * 50)

    except Exception as e:
        print(f"Error fetching metrics: {e}")


if __name__ == "__main__":
    while True:
        send_to_zabbix()
        time.sleep(10)
