def test_port_suggestions(client):
    info = client.ports(shdr_count=3)
    assert info.suggested.agent >= 5000
    assert len(info.suggested.shdr) == 3
    # Suggestions must be mutually distinct and not already used.
    all_suggested = [info.suggested.agent, *info.suggested.shdr]
    assert len(set(all_suggested)) == len(all_suggested)
    assert not set(all_suggested) & set(info.used)


def test_zero_shdr_ports(client):
    info = client.ports(shdr_count=0)
    assert info.suggested.shdr == []
