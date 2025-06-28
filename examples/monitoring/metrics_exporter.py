#!/usr/bin/env python3
"""
Simple HTTP metrics exporter for the Cliffracer demo
Exposes business metrics in a format that's easy to visualize
"""

import asyncio
import json
import time
from collections import deque

from aiohttp import web


class MetricsCollector:
    def __init__(self):
        self.metrics = {
            "orders_created": 0,
            "orders_failed": 0,
            "payments_processed": 0,
            "payments_failed": 0,
            "inventory_reserved": 0,
            "notifications_sent": 0,
            "message_latencies": deque(maxlen=100),
            "order_amounts": deque(maxlen=100),
        }
        self.start_time = time.time()
        self.last_metrics = {}

    def update_from_log(self, log_entry):
        """Parse log entry and update metrics"""
        try:
            data = json.loads(log_entry)
            action = data.get("action", "")

            if action == "order_created":
                self.metrics["orders_created"] += 1
                self.metrics["order_amounts"].append(float(data.get("total_amount", 0)))
                if "processing_time_ms" in data:
                    self.metrics["message_latencies"].append(data["processing_time_ms"])

            elif action == "payment_success":
                self.metrics["payments_processed"] += 1

            elif action == "payment_failed":
                self.metrics["payments_failed"] += 1
                self.metrics["orders_failed"] += 1

            elif action == "inventory_reserved":
                self.metrics["inventory_reserved"] += data.get("item_count", 0)

            elif action == "notification_sent":
                self.metrics["notifications_sent"] += 1

        except json.JSONDecodeError:
            pass

    def get_metrics(self):
        """Get current metrics in Prometheus format"""
        uptime = time.time() - self.start_time

        # Calculate rates
        orders_per_minute = (self.metrics["orders_created"] / uptime) * 60 if uptime > 0 else 0

        # Calculate averages
        avg_latency = (
            sum(self.metrics["message_latencies"]) / len(self.metrics["message_latencies"])
            if self.metrics["message_latencies"]
            else 0
        )
        avg_order_value = (
            sum(self.metrics["order_amounts"]) / len(self.metrics["order_amounts"])
            if self.metrics["order_amounts"]
            else 0
        )

        # Payment success rate
        total_payments = self.metrics["payments_processed"] + self.metrics["payments_failed"]
        payment_success_rate = (
            (self.metrics["payments_processed"] / total_payments * 100) if total_payments > 0 else 0
        )

        return {
            "uptime_seconds": uptime,
            "orders_total": self.metrics["orders_created"],
            "orders_failed_total": self.metrics["orders_failed"],
            "orders_per_minute": round(orders_per_minute, 2),
            "payments_total": self.metrics["payments_processed"],
            "payments_failed_total": self.metrics["payments_failed"],
            "payment_success_rate": round(payment_success_rate, 2),
            "inventory_items_reserved": self.metrics["inventory_reserved"],
            "notifications_sent_total": self.metrics["notifications_sent"],
            "avg_message_latency_ms": round(avg_latency, 3),
            "avg_order_value": round(avg_order_value, 2),
        }


collector = MetricsCollector()


async def metrics_handler(request):
    """Prometheus-style metrics endpoint"""
    metrics = collector.get_metrics()

    # Format as Prometheus metrics
    output = []
    output.append("# HELP cliffracer_uptime_seconds Time since exporter started")
    output.append("# TYPE cliffracer_uptime_seconds counter")
    output.append(f"cliffracer_uptime_seconds {metrics['uptime_seconds']}")

    output.append("# HELP cliffracer_orders_total Total number of orders created")
    output.append("# TYPE cliffracer_orders_total counter")
    output.append(f"cliffracer_orders_total {metrics['orders_total']}")

    output.append("# HELP cliffracer_orders_per_minute Orders created per minute")
    output.append("# TYPE cliffracer_orders_per_minute gauge")
    output.append(f"cliffracer_orders_per_minute {metrics['orders_per_minute']}")

    output.append("# HELP cliffracer_payment_success_rate Payment success rate percentage")
    output.append("# TYPE cliffracer_payment_success_rate gauge")
    output.append(f"cliffracer_payment_success_rate {metrics['payment_success_rate']}")

    output.append("# HELP cliffracer_avg_latency_ms Average message processing latency")
    output.append("# TYPE cliffracer_avg_latency_ms gauge")
    output.append(f"cliffracer_avg_latency_ms {metrics['avg_message_latency_ms']}")

    return web.Response(text="\n".join(output) + "\n", content_type="text/plain")


async def json_metrics_handler(request):
    """JSON metrics endpoint for easy consumption"""
    metrics = collector.get_metrics()
    return web.json_response(metrics)


async def dashboard_handler(request):
    """Simple HTML dashboard"""
    metrics = collector.get_metrics()

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Cliffracer Metrics Dashboard</title>
        <meta http-equiv="refresh" content="5">
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
            .container {{ max-width: 1200px; margin: 0 auto; }}
            h1 {{ color: #333; }}
            .metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; }}
            .metric {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
            .metric h3 {{ margin: 0 0 10px 0; color: #666; font-size: 14px; }}
            .metric .value {{ font-size: 32px; font-weight: bold; color: #2196F3; }}
            .metric .unit {{ font-size: 14px; color: #999; }}
            .success {{ color: #4CAF50; }}
            .warning {{ color: #FF9800; }}
            .error {{ color: #F44336; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🚀 Cliffracer E-Commerce Metrics</h1>
            <p>Auto-refreshes every 5 seconds</p>

            <div class="metrics">
                <div class="metric">
                    <h3>Orders Created</h3>
                    <div class="value">{metrics["orders_total"]}</div>
                    <div class="unit">{metrics["orders_per_minute"]} per minute</div>
                </div>

                <div class="metric">
                    <h3>Payment Success Rate</h3>
                    <div class="value {"success" if metrics["payment_success_rate"] > 80 else "warning"}">{metrics["payment_success_rate"]}%</div>
                    <div class="unit">{metrics["payments_total"]} successful / {metrics["payments_failed_total"]} failed</div>
                </div>

                <div class="metric">
                    <h3>Average Latency</h3>
                    <div class="value {"success" if metrics["avg_message_latency_ms"] < 1 else "warning"}">{metrics["avg_message_latency_ms"]}</div>
                    <div class="unit">milliseconds</div>
                </div>

                <div class="metric">
                    <h3>Inventory Reserved</h3>
                    <div class="value">{metrics["inventory_items_reserved"]}</div>
                    <div class="unit">items</div>
                </div>

                <div class="metric">
                    <h3>Notifications Sent</h3>
                    <div class="value">{metrics["notifications_sent_total"]}</div>
                    <div class="unit">emails/SMS</div>
                </div>

                <div class="metric">
                    <h3>Average Order Value</h3>
                    <div class="value">${metrics["avg_order_value"]}</div>
                    <div class="unit">per order</div>
                </div>
            </div>

            <h2 style="margin-top: 40px;">📊 Real-time Performance</h2>
            <p>Message processing: <strong>{metrics["avg_message_latency_ms"]}ms</strong> average (sub-millisecond is excellent!)</p>
            <p>System uptime: <strong>{int(metrics["uptime_seconds"])}s</strong></p>
        </div>
    </body>
    </html>
    """

    return web.Response(text=html, content_type="text/html")


async def log_reader():
    """Read logs from stdin and update metrics"""
    while True:
        try:
            line = await asyncio.get_event_loop().run_in_executor(None, input)
            collector.update_from_log(line)
        except EOFError:
            break
        except Exception as e:
            print(f"Error reading log: {e}")


async def start_server():
    """Start the metrics server"""
    app = web.Application()
    app.router.add_get("/metrics", metrics_handler)
    app.router.add_get("/json", json_metrics_handler)
    app.router.add_get("/", dashboard_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", 9090)
    await site.start()

    print("📊 Metrics server started at http://localhost:9090")
    print("   - Prometheus metrics: http://localhost:9090/metrics")
    print("   - JSON metrics: http://localhost:9090/json")
    print("   - Dashboard: http://localhost:9090/")

    # Keep server running
    await asyncio.Event().wait()


async def main():
    """Run both log reader and server"""
    await asyncio.gather(log_reader(), start_server())


if __name__ == "__main__":
    asyncio.run(main())
