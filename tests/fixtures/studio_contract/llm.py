"""Frozen Studio contract excerpts for AST-isolated tests, not a runnable service.

These fixtures do not verify the currently installed external Studio release.
"""
class SystemdLLM:

    async def ensure_image_admission(self):
        status = await self.gate('GET')
        holds = status.get('holds')
        if type(status.get('protocol')) is not int or status['protocol'] != 3 or status.get('durable') is not True or ('recovery_exclusive_hold' not in status) or (status['recovery_exclusive_hold'] is not None) or (not isinstance(holds, list)) or any((not isinstance(x, str) for x in holds)):
            raise RuntimeError('Owned durable admission control is unavailable')
        if any((hold == 'startup' or hold.startswith(('studio-', 'switch-')) for hold in holds)):
            raise LockBusyError('Admission recovery must finish before image execution')

    async def owned_control_identity(self):
        headers = {'Authorization': f'Bearer {self.s.admission_key}'}
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.get(self.s.admission_url.rstrip('/') + '/identity', headers=headers)
            response.raise_for_status()
            value = response.json()
            return isinstance(value, dict) and value.keys() == {'role', 'protocol'} and (value['role'] == 'studio') and (type(value['protocol']) is int) and (value['protocol'] == 3)
