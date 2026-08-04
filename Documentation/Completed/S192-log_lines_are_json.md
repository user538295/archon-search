## Bug: `--log-format json` wizard flag writes JSON-formatted log entries

**ID**: S192-log_lines_are_json
**Scenario**: S192
**Severity**: medium
**Version**: archon-search, version 26.8.1845

### What happened
AssertionError: log line is not a JSON object: 'INFO:     Started server process [6216]'
assert False

### What should happen
- Wizard exits 0.
- Each line of the log file is a valid JSON object (parseable, starts with `{`).

### Steps to reproduce
1. `archon-search wizard --profile minimal --non-interactive --skip-preload --log-format json`
2. `head -5 ~/.archon-search/logs/archon-search.log`

### Evidence
```
E   AssertionError: log line is not a JSON object: 'INFO:     Started server process [6216]'
E   assert False
E    +  where False = <built-in method startswith of str object at 0x109756a60>('{')
E    +    where <built-in method startswith of str object at 0x109756a60> = 'INFO:     Started server process [6216]'.startswith
E    +      where 'INFO:     Started server process [6216]' = <built-in method lstrip of str object at 0x109756a60>()
E    +        where <built-in method lstrip of str object at 0x109756a60> = 'INFO:     Started server process [6216]'.lstrip
```
