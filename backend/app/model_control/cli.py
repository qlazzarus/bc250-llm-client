"""ai-model CLI; all control passes through the shared worker."""
import argparse
import json
import asyncio
import sys
from uuid import uuid4

from .client import ModelControlClient, WorkerUnavailable

SOCKET = '/run/bc250-model-control/control.sock'
TERMINAL = {'succeeded', 'failed', 'failed_rolled_back', 'recovery_required'}


async def run(args, client=None):
    client = client or ModelControlClient(SOCKET)
    try:
        if args.command == 'list':
            result = await client.request('models')
            if 'error' not in result:
                for model in result['models']:
                    print(model['name'])
        elif args.command == 'status' and args.operation:
            result = await client.request('get', id=args.operation)
            if 'error' not in result:
                print(f"operation={result['id']} state={result['state']} error={result.get('error_code')}")
        elif args.command == 'status':
            result = await client.request('models')
            if 'error' not in result:
                print(f"configured={result['configured_model']} loaded={result['loaded_model']} ready={result['ready']}")
                current = await client.request('current')
                if 'error' in current:
                    result = current
                elif current['operation']:
                    print(f"operation={current['operation']['id']} state={current['operation']['state']}")
        elif args.command == 'inspect-recovery':
            result = await client.request('inspect_recovery')
            if 'error' not in result:
                print(json.dumps(result))
        elif args.command in {'recover', 'resolve-verified'}:
            result = await client.request(args.command.replace('-', '_'), id=args.operation)
            if 'error' not in result:
                print(f"operation={result['id']} state={result['state']} error={result.get('error_code')}")
                return 0 if result['state'] in {'failed', 'failed_rolled_back'} else 2
        else:
            key = args.key or uuid4().hex
            print(f'idempotency_key={key}', flush=True)
            result = await client.request('submit', model=args.model, idempotency_key=key)
            if 'error' in result:
                print(f"request failed: {result['error']}", file=sys.stderr)
                return 1
            operation_id = result['id']
            print(f'operation_id={operation_id}', flush=True)
            while True:
                print(f"state={result['state']}", flush=True)
                if result['state'] in TERMINAL:
                    return 0 if result['state'] == 'succeeded' else 2 if result['state'] == 'recovery_required' else 1
                await asyncio.sleep(1)
                result = await client.request('get', id=operation_id)
                if 'error' in result:
                    print('Operation status unavailable; inspect using the operation ID.', file=sys.stderr)
                    return 2
        if 'error' in result:
            print(f"worker error: {result['error']}", file=sys.stderr)
            return 1
        return 0
    except WorkerUnavailable:
        print('Worker unavailable; outcome may be unknown. Inspect the operation before retrying.', file=sys.stderr)
        return 2


def main():
    parser = argparse.ArgumentParser(prog='ai-model')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list')
    status = commands.add_parser('status'); status.add_argument('--operation')
    commands.add_parser('inspect-recovery')
    for name in ('recover', 'resolve-verified'):
        command = commands.add_parser(name)
        command.add_argument('operation')
    switch = commands.add_parser('switch')
    switch.add_argument('model'); switch.add_argument('--key')
    return asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    raise SystemExit(main())
