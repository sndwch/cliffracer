"""
Banking Transfer Saga Example

Demonstrates a simple money transfer between accounts using the Saga pattern
with automatic compensation for maintaining consistency.
"""

import asyncio
from datetime import datetime
from typing import Dict, Any

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.patterns.saga import SagaCoordinator, SagaParticipant, SagaStep


class AccountService(CliffracerService, SagaParticipant):
    """Account management service"""
    
    def __init__(self):
        config = ServiceConfig(name="account_service")
        CliffracerService.__init__(self, config)
        SagaParticipant.__init__(self, self)
        
        # Simulated account balances
        self.accounts = {
            "ACC-001": {"balance": 1000.00, "holder": "Alice", "transactions": []},
            "ACC-002": {"balance": 500.00, "holder": "Bob", "transactions": []},
            "ACC-003": {"balance": 750.00, "holder": "Charlie", "transactions": []},
        }
        
        # Transaction log
        self.transactions = {}
    
    def _register_handlers(self):
        """Register saga handlers"""
        
        @self.rpc
        async def debit_account(saga_id: str, correlation_id: str, step: str, data: dict) -> dict:
            """Debit money from source account"""
            try:
                account_id = data["from_account"]
                amount = data["amount"]
                
                if account_id not in self.accounts:
                    return {"error": f"Account {account_id} not found"}
                
                account = self.accounts[account_id]
                
                # Check sufficient balance
                if account["balance"] < amount:
                    return {"error": f"Insufficient balance. Available: ${account['balance']:.2f}"}
                
                # Debit the account
                account["balance"] -= amount
                transaction_id = f"TXN-{saga_id[:8]}-DEBIT"
                
                # Record transaction
                self.transactions[transaction_id] = {
                    "id": transaction_id,
                    "account": account_id,
                    "type": "debit",
                    "amount": amount,
                    "saga_id": saga_id,
                    "timestamp": datetime.now().isoformat()
                }
                
                account["transactions"].append(transaction_id)
                
                print(f"💸 Debited ${amount:.2f} from {account_id} (Balance: ${account['balance']:.2f})")
                
                return {
                    "result": {
                        "transaction_id": transaction_id,
                        "new_balance": account["balance"]
                    }
                }
            except Exception as e:
                return {"error": str(e)}
        
        @self.rpc
        async def credit_account(saga_id: str, correlation_id: str, step: str, data: dict) -> dict:
            """Credit money to destination account"""
            try:
                account_id = data["to_account"]
                amount = data["amount"]
                
                if account_id not in self.accounts:
                    return {"error": f"Account {account_id} not found"}
                
                account = self.accounts[account_id]
                
                # Credit the account
                account["balance"] += amount
                transaction_id = f"TXN-{saga_id[:8]}-CREDIT"
                
                # Record transaction
                self.transactions[transaction_id] = {
                    "id": transaction_id,
                    "account": account_id,
                    "type": "credit",
                    "amount": amount,
                    "saga_id": saga_id,
                    "timestamp": datetime.now().isoformat()
                }
                
                account["transactions"].append(transaction_id)
                
                print(f"💰 Credited ${amount:.2f} to {account_id} (Balance: ${account['balance']:.2f})")
                
                return {
                    "result": {
                        "transaction_id": transaction_id,
                        "new_balance": account["balance"]
                    }
                }
            except Exception as e:
                return {"error": str(e)}
        
        @self.rpc
        async def reverse_debit(saga_id: str, correlation_id: str, step: str, data: dict, original_result: dict) -> dict:
            """Reverse a debit transaction (compensation)"""
            try:
                transaction_id = original_result["transaction_id"]
                original_txn = self.transactions.get(transaction_id)
                
                if not original_txn:
                    return {"error": "Original transaction not found"}
                
                account_id = original_txn["account"]
                amount = original_txn["amount"]
                
                # Credit back the amount
                self.accounts[account_id]["balance"] += amount
                
                # Record reversal
                reversal_id = f"{transaction_id}-REVERSAL"
                self.transactions[reversal_id] = {
                    "id": reversal_id,
                    "account": account_id,
                    "type": "reversal",
                    "amount": amount,
                    "original_transaction": transaction_id,
                    "saga_id": saga_id,
                    "timestamp": datetime.now().isoformat()
                }
                
                print(f"↩️  Reversed debit of ${amount:.2f} from {account_id} (Balance: ${self.accounts[account_id]['balance']:.2f})")
                
                return {"result": {"status": "reversed", "reversal_id": reversal_id}}
            except Exception as e:
                return {"error": str(e)}
        
        @self.rpc
        async def reverse_credit(saga_id: str, correlation_id: str, step: str, data: dict, original_result: dict) -> dict:
            """Reverse a credit transaction (compensation)"""
            try:
                transaction_id = original_result["transaction_id"]
                original_txn = self.transactions.get(transaction_id)
                
                if not original_txn:
                    return {"error": "Original transaction not found"}
                
                account_id = original_txn["account"]
                amount = original_txn["amount"]
                
                # Debit back the amount
                self.accounts[account_id]["balance"] -= amount
                
                # Record reversal
                reversal_id = f"{transaction_id}-REVERSAL"
                self.transactions[reversal_id] = {
                    "id": reversal_id,
                    "account": account_id,
                    "type": "reversal",
                    "amount": amount,
                    "original_transaction": transaction_id,
                    "saga_id": saga_id,
                    "timestamp": datetime.now().isoformat()
                }
                
                print(f"↩️  Reversed credit of ${amount:.2f} from {account_id} (Balance: ${self.accounts[account_id]['balance']:.2f})")
                
                return {"result": {"status": "reversed", "reversal_id": reversal_id}}
            except Exception as e:
                return {"error": str(e)}
        
        @self.rpc
        async def get_balance(account_id: str) -> dict:
            """Get account balance"""
            if account_id not in self.accounts:
                return {"error": f"Account {account_id} not found"}
            
            account = self.accounts[account_id]
            return {
                "account_id": account_id,
                "holder": account["holder"],
                "balance": account["balance"],
                "transactions": len(account["transactions"])
            }


class NotificationService(CliffracerService, SagaParticipant):
    """Notification service for transaction alerts"""
    
    def __init__(self):
        config = ServiceConfig(name="notification_service")
        CliffracerService.__init__(self, config)
        SagaParticipant.__init__(self, self)
    
    def _register_handlers(self):
        """Register saga handlers"""
        
        @self.rpc
        async def notify_transfer(saga_id: str, correlation_id: str, step: str, data: dict) -> dict:
            """Send transfer notification"""
            try:
                # Simulate notification
                print(f"📧 Sending notifications:")
                print(f"   To sender: Transfer of ${data['amount']:.2f} sent to {data['to_account']}")
                print(f"   To recipient: Received ${data['amount']:.2f} from {data['from_account']}")
                
                return {
                    "result": {
                        "notifications_sent": 2,
                        "status": "delivered"
                    }
                }
            except Exception as e:
                return {"error": str(e)}
        
        @self.rpc
        async def notify_reversal(saga_id: str, correlation_id: str, step: str, data: dict, original_result: dict) -> dict:
            """Send reversal notification (compensation)"""
            try:
                print(f"📧 Sending reversal notifications:")
                print(f"   Transfer of ${data['amount']:.2f} has been cancelled")
                
                return {"result": {"status": "notified"}}
            except Exception as e:
                return {"error": str(e)}


class BankingService(CliffracerService):
    """Banking service orchestrator"""
    
    def __init__(self):
        config = ServiceConfig(name="banking_service")
        super().__init__(config)
        
        self.coordinator = SagaCoordinator(self)
        
        # Define money transfer saga
        self.coordinator.define_saga("money_transfer", [
            SagaStep(
                name="debit_source",
                service="account_service",
                action="debit_account",
                compensation="reverse_debit",
                timeout=10.0
            ),
            SagaStep(
                name="credit_destination",
                service="account_service",
                action="credit_account",
                compensation="reverse_credit",
                timeout=10.0
            ),
            SagaStep(
                name="send_notifications",
                service="notification_service",
                action="notify_transfer",
                compensation="notify_reversal",
                timeout=5.0
            )
        ])
    
    @property
    def rpc(self):
        """RPC decorator"""
        return self._rpc_decorator
    
    def _rpc_decorator(self, func):
        """Register RPC handler"""
        self._rpc_handlers[func.__name__] = func
        return func
    
    @rpc
    async def transfer_money(
        self,
        from_account: str,
        to_account: str,
        amount: float,
        description: str = "Transfer"
    ) -> dict:
        """Transfer money between accounts"""
        print(f"\n💳 Initiating transfer: ${amount:.2f}")
        print(f"   From: {from_account}")
        print(f"   To: {to_account}")
        print(f"   Description: {description}")
        print("-" * 40)
        
        result = await self.coordinator._start_saga("money_transfer", {
            "from_account": from_account,
            "to_account": to_account,
            "amount": amount,
            "description": description
        })
        
        return result


async def demonstrate_banking_saga():
    """Demonstrate the banking transfer saga"""
    # Start services
    account_service = AccountService()
    notification_service = NotificationService()
    banking_service = BankingService()
    
    services = [account_service, notification_service, banking_service]
    
    # Run services
    tasks = []
    for service in services:
        task = asyncio.create_task(service.run())
        tasks.append(task)
    
    # Wait for services to start
    await asyncio.sleep(2)
    
    print("🏦 Banking Transfer Saga Demo")
    print("=" * 60)
    
    # Show initial balances
    print("\n📊 Initial Account Balances:")
    for acc_id in ["ACC-001", "ACC-002", "ACC-003"]:
        balance = await account_service.get_balance(acc_id)
        print(f"   {acc_id} ({balance['holder']}): ${balance['balance']:.2f}")
    
    # Example 1: Successful transfer
    print("\n" + "="*60)
    print("EXAMPLE 1: Successful Transfer")
    print("="*60)
    
    result1 = await banking_service.transfer_money(
        from_account="ACC-001",
        to_account="ACC-002",
        amount=200.00,
        description="Payment for services"
    )
    
    await asyncio.sleep(3)
    
    # Check saga status
    status = await banking_service.rpc_call(
        "banking_service.get_saga_status",
        {"saga_id": result1["saga_id"]}
    )
    
    if status.get("state") == "COMPLETED":
        print("\n✅ Transfer completed successfully!")
    
    # Show updated balances
    print("\n📊 Updated Balances:")
    for acc_id in ["ACC-001", "ACC-002"]:
        balance = await account_service.get_balance(acc_id)
        print(f"   {acc_id} ({balance['holder']}): ${balance['balance']:.2f}")
    
    # Example 2: Failed transfer (insufficient funds)
    print("\n" + "="*60)
    print("EXAMPLE 2: Failed Transfer (Insufficient Funds)")
    print("="*60)
    
    result2 = await banking_service.transfer_money(
        from_account="ACC-002",
        to_account="ACC-003",
        amount=1000.00,  # More than available balance
        description="Large transfer"
    )
    
    await asyncio.sleep(3)
    
    # Check saga status
    status = await banking_service.rpc_call(
        "banking_service.get_saga_status",
        {"saga_id": result2["saga_id"]}
    )
    
    if status.get("state") == "FAILED":
        print("\n❌ Transfer failed due to insufficient funds")
        print("   No money was moved")
    
    # Example 3: Transfer with simulated failure
    print("\n" + "="*60)
    print("EXAMPLE 3: Transfer with Credit Failure")
    print("="*60)
    
    # Temporarily remove destination account to simulate failure
    acc_backup = account_service.accounts.pop("ACC-003", None)
    
    result3 = await banking_service.transfer_money(
        from_account="ACC-001",
        to_account="ACC-003",  # This account is temporarily missing
        amount=100.00,
        description="Transfer to missing account"
    )
    
    await asyncio.sleep(3)
    
    # Restore account
    if acc_backup:
        account_service.accounts["ACC-003"] = acc_backup
    
    # Check saga status
    status = await banking_service.rpc_call(
        "banking_service.get_saga_status",
        {"saga_id": result3["saga_id"]}
    )
    
    if status.get("state") == "COMPENSATED":
        print("\n❌ Transfer failed and was compensated")
        print("   The debit was automatically reversed")
    
    # Final balances
    print("\n📊 Final Account Balances:")
    for acc_id in ["ACC-001", "ACC-002", "ACC-003"]:
        balance = await account_service.get_balance(acc_id)
        print(f"   {acc_id} ({balance['holder']}): ${balance['balance']:.2f}")
    
    print("\n\nPress Ctrl+C to stop...")
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print("\nShutting down services...")
        for service in services:
            service.stop()


if __name__ == "__main__":
    asyncio.run(demonstrate_banking_saga())