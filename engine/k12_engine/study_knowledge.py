"""Read the shared Study knowledge catalogue through fixed CLI commands."""
from __future__ import annotations

import re

from . import study_transport

SAFE_VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}')


def _catalog_result(args: dict, value: object) -> dict:
    if not isinstance(value, dict):
        raise study_transport.TransportError('blocked', 'invalid shared knowledge catalog response', code='BAD_KNOWLEDGE_RESPONSE')
    version = value.get('version')
    if args['action'] == 'knowledge_manifest':
        valid_version = version is None or isinstance(version, str) and SAFE_VERSION.fullmatch(version)
        valid_counts = all(type(value.get(key)) is int and value[key] >= 0 for key in ('nodeCount', 'knowledgeCount', 'edgeCount'))
        if ('version' not in value or not valid_version or value.get('schemaVersion') != 'k12-knowledge-catalog/v1'
                or not isinstance(value.get('sources'), list) or not isinstance(value.get('books'), list) or not valid_counts):
            raise study_transport.TransportError('blocked', 'invalid shared knowledge catalog manifest', code='BAD_KNOWLEDGE_RESPONSE')
        return value
    if not isinstance(version, str) or not SAFE_VERSION.fullmatch(version):
        raise study_transport.TransportError('blocked', 'shared knowledge response has no valid version', code='BAD_KNOWLEDGE_RESPONSE')
    if args.get('version') is not None and args['version'] != version:
        raise study_transport.TransportError('conflict', 'knowledge catalog version changed; read manifest and retry', code='KNOWLEDGE_VERSION_MISMATCH')
    if args['action'] == 'knowledge_search' and (not isinstance(value.get('items'), list) or type(value.get('truncated')) is not bool):
        raise study_transport.TransportError('blocked', 'invalid shared knowledge search response', code='BAD_KNOWLEDGE_RESPONSE')
    if args['action'] == 'knowledge_node' and (not isinstance(value.get('node'), dict)
                                               or value['node'].get('id') != args['resourceId']):
        raise study_transport.TransportError('blocked', 'shared knowledge node does not match the requested ID', code='BAD_KNOWLEDGE_RESPONSE')
    if args['action'] == 'knowledge_graph' and (value.get('centerId') != args['resourceId']
                                                or not isinstance(value.get('nodes'), list)
                                                or not isinstance(value.get('edges'), list)
                                                or type(value.get('truncated')) is not bool):
        raise study_transport.TransportError('blocked', 'invalid shared knowledge graph response', code='BAD_KNOWLEDGE_RESPONSE')
    return value


def run(args: dict) -> object:
    allowed = {'action', 'query', 'resourceId', 'version', 'limit', 'cwd', 'session'}
    if set(args) - allowed or not isinstance(args.get('cwd'), str):
        raise study_transport.TransportError('blocked', 'invalid knowledge request')
    action = args.get('action')
    mapping = {
        'knowledge_manifest': ('knowledge_manifest', ()),
        'knowledge_search': ('knowledge_search', ('query', 'limit', 'version')),
        'knowledge_node': ('knowledge_node', ('resourceId', 'version')),
        'knowledge_graph': ('knowledge_graph', ('resourceId', 'version')),
    }
    if action not in mapping:
        raise study_transport.TransportError('blocked', 'unsupported knowledge action')
    operation, fields = mapping[action]
    if any(key not in fields and key in args for key in ('query', 'resourceId', 'version', 'limit')):
        raise study_transport.TransportError('blocked', 'unexpected knowledge field')
    if action == 'knowledge_search' and not isinstance(args.get('query'), str):
        raise study_transport.TransportError('blocked', 'knowledge search requires a query')
    if action in {'knowledge_node', 'knowledge_graph'} and not isinstance(args.get('resourceId'), str):
        raise study_transport.TransportError('blocked', 'knowledge operation requires a reference')
    binding = study_transport.load_binding(args['cwd'])
    if not binding:
        raise study_transport.TransportError('blocked', 'shared knowledge graph is unavailable: workspace is unbound')
    client = study_transport.Transport(binding)
    value = client.call(operation, **{'resource_id' if key == 'resourceId' else key: args[key]
                                      for key in fields if key in args})
    return _catalog_result(args, value)
