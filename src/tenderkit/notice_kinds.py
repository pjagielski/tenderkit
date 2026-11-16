"""Common procurement notice kinds and the source types that can yield them."""

NOTICE_TYPES = {
    'contract': ('ContractNotice', 'SmallContractNotice', 'CompetitionNotice', 'BKAnnouncement',
                 'cn-standard', 'cn-social', 'pin-cfc-standard'),
    'result': ('TenderResultNotice', 'can-standard'),
    'corrigendum': ('NoticeUpdateNotice', 'corr'),
    'prior_info': ('pin-only', 'pin-rtl'),
}
_KIND_BY_TYPE = {value: kind for kind, values in NOTICE_TYPES.items() for value in values}


def notice_kind(notice):
    if notice.source == 'TED' and notice.raw.get('change-notice-version-identifier'):
        return 'corrigendum'
    return _KIND_BY_TYPE.get(notice.notice_type)


def source_types(kinds):
    types = dict.fromkeys(value for kind in kinds for value in NOTICE_TYPES[kind])
    if 'corrigendum' in kinds:
        # TED revisions may still carry the underlying competition/result type.
        for kind in ('contract', 'result', 'prior_info'):
            types.update(dict.fromkeys(value for value in NOTICE_TYPES[kind] if '-' in value))
    return tuple(types)
