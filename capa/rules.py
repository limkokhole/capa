import uuid
import codecs
import logging
import binascii

import six
import yaml
import ruamel.yaml

import capa.engine
from capa.engine import *
import capa.features
import capa.features.file
import capa.features.function
import capa.features.basicblock
import capa.features.insn
from capa.features import MAX_BYTES_FEATURE_SIZE


logger = logging.getLogger(__name__)


FILE_SCOPE = 'file'
FUNCTION_SCOPE = 'function'
BASIC_BLOCK_SCOPE = 'basic block'


SUPPORTED_FEATURES = {
    FILE_SCOPE: set([
        capa.features.MatchedRule,
        capa.features.file.Export,
        capa.features.file.Import,
        capa.features.file.Section,
        capa.features.Characteristic('embedded pe'),
        capa.features.String,
    ]),
    FUNCTION_SCOPE: set([
        capa.features.MatchedRule,
        capa.features.insn.API,
        capa.features.insn.Number,
        capa.features.String,
        capa.features.Bytes,
        capa.features.insn.Offset,
        capa.features.insn.Mnemonic,
        capa.features.basicblock.BasicBlock,
        capa.features.Characteristic('switch'),
        capa.features.Characteristic('nzxor'),
        capa.features.Characteristic('peb access'),
        capa.features.Characteristic('fs access'),
        capa.features.Characteristic('gs access'),
        capa.features.Characteristic('cross section flow'),
        capa.features.Characteristic('stack string'),
        capa.features.Characteristic('calls from'),
        capa.features.Characteristic('calls to'),
        capa.features.Characteristic('indirect call'),
        capa.features.Characteristic('loop'),
        capa.features.Characteristic('recursive call')
    ]),
    BASIC_BLOCK_SCOPE: set([
        capa.features.MatchedRule,
        capa.features.insn.API,
        capa.features.insn.Number,
        capa.features.String,
        capa.features.Bytes,
        capa.features.insn.Offset,
        capa.features.insn.Mnemonic,
        capa.features.Characteristic('nzxor'),
        capa.features.Characteristic('peb access'),
        capa.features.Characteristic('fs access'),
        capa.features.Characteristic('gs access'),
        capa.features.Characteristic('cross section flow'),
        capa.features.Characteristic('tight loop'),
        capa.features.Characteristic('stack string'),
        capa.features.Characteristic('indirect call')
    ]),
}


class InvalidRule(ValueError):
    def __init__(self, msg):
        super(InvalidRule, self).__init__()
        self.msg = msg

    def __str__(self):
        return 'invalid rule: %s' % (self.msg)

    def __repr__(self):
        return str(self)


class InvalidRuleWithPath(InvalidRule):
    def __init__(self, path, msg):
        super(InvalidRuleWithPath, self).__init__(msg)
        self.path = path
        self.msg = msg
        self.__cause__ = None

    def __str__(self):
        return 'invalid rule: %s: %s' % (self.path, self.msg)


class InvalidRuleSet(ValueError):
    def __init__(self, msg):
        super(InvalidRuleSet, self).__init__()
        self.msg = msg

    def __str__(self):
        return 'invalid rule set: %s' % (self.msg)

    def __repr__(self):
        return str(self)


def ensure_feature_valid_for_scope(scope, feature):
    if isinstance(feature, capa.features.Characteristic):
        if capa.features.Characteristic(feature.name) not in SUPPORTED_FEATURES[scope]:
            raise InvalidRule('feature %s not support for scope %s' % (feature, scope))
    elif not isinstance(feature, tuple(filter(lambda t: isinstance(t, type), SUPPORTED_FEATURES[scope]))):
        raise InvalidRule('feature %s not support for scope %s' % (feature, scope))


def parse_int(s):
    if s.startswith('0x'):
        return int(s, 0x10)
    else:
        return int(s, 10)


def parse_range(s):
    '''
    parse a string "(0, 1)" into a range (min, max).
    min and/or max may by None to indicate an unbound range.
    '''
    # we want to use `{` characters, but this is a dict in yaml.
    if not s.startswith('('):
        raise InvalidRule('invalid range: %s' % (s))

    if not s.endswith(')'):
        raise InvalidRule('invalid range: %s' % (s))

    s = s[len('('):-len(')')]
    min, _, max = s.partition(',')
    min = min.strip()
    max = max.strip()

    if min:
        min = parse_int(min.strip())
        if min < 0:
            raise InvalidRule('range min less than zero')
    else:
        min = None

    if max:
        max = parse_int(max.strip())
        if max < 0:
            raise InvalidRule('range max less than zero')
    else:
        max = None

    if min is not None and max is not None:
        if max < min:
            raise InvalidRule('range max less than min')

    return min, max


def parse_feature(key):
    # keep this in sync with supported features
    if key == 'api':
        return capa.features.insn.API
    elif key == 'string':
        return capa.features.String
    elif key == 'bytes':
        return capa.features.Bytes
    elif key == 'number':
        return capa.features.insn.Number
    elif key == 'offset':
        return capa.features.insn.Offset
    elif key == 'mnemonic':
        return capa.features.insn.Mnemonic
    elif key == 'basic blocks':
        return capa.features.basicblock.BasicBlock
    elif key.startswith('characteristic(') and key.endswith(')'):
        characteristic = key[len('characteristic('):-len(')')]
        return lambda v: capa.features.Characteristic(characteristic, v)
    elif key == 'export':
        return capa.features.file.Export
    elif key == 'import':
        return capa.features.file.Import
    elif key == 'section':
        return capa.features.file.Section
    elif key == 'match':
        return capa.features.MatchedRule
    else:
        raise InvalidRule('unexpected statement: %s' % key)


def parse_symbol(s, value_type):
    '''
    s can be an int or a string
    '''
    if isinstance(s, str) and '=' in s:
        value, symbol = s.split('=', 1)
        symbol = symbol.strip()
        if symbol == '':
            raise InvalidRule('unexpected value: "%s", symbol name cannot be empty' % s)
    else:
        value = s
        symbol = None

    if isinstance(value, str):
        if value_type == 'bytes':
            try:
                value = codecs.decode(value.replace(' ', ''), 'hex')
            # TODO: Remove TypeError when Python2 is not used anymore
            except (TypeError, binascii.Error):
                raise InvalidRule('unexpected bytes value: "%s", must be a valid hex sequence' % value)

            if len(value) > MAX_BYTES_FEATURE_SIZE:
                raise InvalidRule('unexpected bytes value: byte sequences must be no larger than %s bytes' %
                                  MAX_BYTES_FEATURE_SIZE)
        else:
            try:
                value = parse_int(value)
            except ValueError:
                raise InvalidRule('unexpected value: "%s", must begin with numerical value' % value)

    return value, symbol


def build_statements(d, scope):
    if len(d.keys()) != 1:
        raise InvalidRule('too many statements')

    key = list(d.keys())[0]
    if key == 'and':
        return And(*[build_statements(dd, scope) for dd in d[key]])
    elif key == 'or':
        return Or(*[build_statements(dd, scope) for dd in d[key]])
    elif key == 'not':
        if len(d[key]) != 1:
            raise InvalidRule('not statement must have exactly one child statement')
        return Not(*[build_statements(dd, scope) for dd in d[key]])
    elif key.endswith(' or more'):
        count = int(key[:-len('or more')])
        return Some(count, *[build_statements(dd, scope) for dd in d[key]])
    elif key == 'optional':
        # `optional` is an alias for `0 or more`
        # which is useful for documenting behaviors,
        # like with `write file`, we might say that `WriteFile` is optionally found alongside `CreateFileA`.
        return Some(0, *[build_statements(dd, scope) for dd in d[key]])

    elif key == 'function':
        if scope != FILE_SCOPE:
            raise InvalidRule('function subscope supported only for file scope')

        if len(d[key]) != 1:
            raise InvalidRule('subscope must have exactly one child statement')

        return Subscope(FUNCTION_SCOPE, *[build_statements(dd, FUNCTION_SCOPE) for dd in d[key]])

    elif key == 'basic block':
        if scope != FUNCTION_SCOPE:
            raise InvalidRule('basic block subscope supported only for function scope')

        if len(d[key]) != 1:
            raise InvalidRule('subscope must have exactly one child statement')

        return Subscope(BASIC_BLOCK_SCOPE, *[build_statements(dd, BASIC_BLOCK_SCOPE) for dd in d[key]])

    elif key.startswith('count(') and key.endswith(')'):
        # e.g.:
        #
        #     count(basic block)
        #     count(mnemonic(mov))
        #     count(characteristic(nzxor))

        term = key[len('count('):-len(')')]

        if term.startswith('characteristic('):
            # characteristic features are specified a bit specially:
            # they simply indicate the presence of something unusual/interesting,
            # and we embed the name in the feature name, like `characteristic(nzxor)`.
            #
            # when we're dealing with counts, like `count(characteristic(nzxor))`,
            # we can simply extract the feature and assume we're looking for `True` values.
            Feature = parse_feature(term)
            feature = Feature(True)
            ensure_feature_valid_for_scope(scope, feature)
        else:
            # however, for remaining counted features, like `count(mnemonic(mov))`,
            # we have to jump through hoops.
            #
            # when looking for the existance of such a feature, our rule might look like:
            #     - mnemonic: mov
            #
            # but here we deal with the form: `mnemonic(mov)`.
            term, _, arg = term.partition('(')
            Feature = parse_feature(term)

            if arg:
                arg = arg[:-len(')')]
                # can't rely on yaml parsing ints embedded within strings
                # like:
                #
                #     count(offset(0xC))
                #     count(number(0x11223344))
                #     count(number(0x100 = symbol name))
                if term in ('number', 'offset', 'bytes'):
                    value, symbol = parse_symbol(arg, term)
                    feature = Feature(value, symbol)
                else:
                    # arg is string, like:
                    #
                    #     count(mnemonic(mov))
                    #     count(string(error))
                    # TODO: what about embedded newlines?
                    feature = Feature(arg)
            else:
                feature = Feature()
            ensure_feature_valid_for_scope(scope, feature)

        count = d[key]
        if isinstance(count, int):
            return Range(feature, min=count, max=count)
        elif count.endswith(' or more'):
            min = parse_int(count[:-len(' or more')])
            max = None
            return Range(feature, min=min, max=max)
        elif count.endswith(' or fewer'):
            min = None
            max = parse_int(count[:-len(' or fewer')])
            return Range(feature, min=min, max=max)
        elif count.startswith('('):
            min, max = parse_range(count)
            return Range(feature, min=min, max=max)
        else:
            raise InvalidRule('unexpected range: %s' % (count))
    elif key == 'string' and d[key].startswith('/') and (d[key].endswith('/') or d[key].endswith('/i')):
        try:
            return Regex(d[key])
        except re.error:
            if d[key].endswith('/i'):
                d[key] = d[key][:-len('i')]
            raise InvalidRule('invalid regular expression: %s it should use Python syntax, try it at https://pythex.org' % d[key])
    else:
        Feature = parse_feature(key)
        if key in ('number', 'offset', 'bytes'):
            # parse numbers with symbol description, e.g. 0x4550 = IMAGE_DOS_SIGNATURE
            # or regular numbers, e.g. 37
            value, symbol = parse_symbol(d[key], key)
            feature = Feature(value, symbol)
        else:
            feature = Feature(d[key])
        ensure_feature_valid_for_scope(scope, feature)
        return feature


def first(s):
    return s[0]


def second(s):
    return s[1]


class Rule(object):
    def __init__(self, name, scope, statement, meta, definition=''):
        super(Rule, self).__init__()
        self.name = name
        self.scope = scope
        self.statement = statement
        self.meta = meta
        self.definition = definition

    def __str__(self):
        return 'Rule(name=%s)' % (self.name)

    def __repr__(self):
        return 'Rule(scope=%s, name=%s)' % (self.scope, self.name)

    def get_dependencies(self):
        '''
        fetch the names of rules this rule relies upon.
        these are only the direct dependencies; a user must
         compute the transitive dependency graph themself, if they want it.

        Returns:
          List[str]: names of rules upon which this rule depends.
        '''
        deps = set([])

        def rec(statement):
            if isinstance(statement, capa.features.MatchedRule):
                deps.add(statement.rule_name)

            elif isinstance(statement, Statement):
                for child in statement.get_children():
                    rec(child)

            # else: might be a Feature, etc.
            # which we don't care about here.

        rec(self.statement)
        return deps

    def _extract_subscope_rules_rec(self, statement):
        if isinstance(statement, Statement):
            # for each child that is a subscope,
            for subscope in filter(lambda statement: isinstance(statement, capa.engine.Subscope), statement.get_children()):

                # create a new rule from it.
                # the name is a randomly generated, hopefully unique value.
                # ideally, this won't every be rendered to a user.
                name = self.name + '/' + uuid.uuid4().hex
                new_rule = Rule(name, subscope.scope, subscope.child, {
                    'name': name,
                    'scope': subscope.scope,
                    # these derived rules are never meant to be inspected separately,
                    # they are dependencies for the parent rule,
                    # so mark it as such.
                    'lib': True,
                    # metadata that indicates this is derived from a subscope statement
                    'capa/subscope-rule': True,
                    # metadata that links the child rule the parent rule
                    'capa/parent': self.name,
                })

                # update the existing statement to `match` the new rule
                new_node = capa.features.MatchedRule(name)
                statement.replace_child(subscope, new_node)

                # and yield the new rule to our caller
                yield new_rule

            # now recurse to other nodes in the logic tree.
            # note: we cannot recurse into the subscope sub-tree,
            #  because its been replaced by a `match` statement.
            for child in statement.get_children():
                for new_rule in self._extract_subscope_rules_rec(child):
                    yield new_rule

    def extract_subscope_rules(self):
        '''
        scan through the statements of this rule,
        replacing subscope statements with `match` references to a newly created rule,
        which are yielded from this routine.

        note: this mutates the current rule.

        example::

            for derived_rule in rule.extract_subscope_rules():
                assert derived_rule.meta['capa/parent'] == rule.name
        '''

        # recurse through statements
        # when encounter Subscope statement
        #   create new transient rule
        #   copy logic into the new rule
        #   replace old node with reference to new rule
        #   yield new rule

        for new_rule in self._extract_subscope_rules_rec(self.statement):
            yield new_rule

    def evaluate(self, features):
        return self.statement.evaluate(features)

    @classmethod
    def from_dict(cls, d, s):
        name = d['rule']['meta']['name']
        # if scope is not specified, default to function scope.
        # this is probably the mode that rule authors will start with.
        scope = d['rule']['meta'].get('scope', FUNCTION_SCOPE)
        statements = d['rule']['features']

        # the rule must start with a single logic node.
        # doing anything else is too implicit and difficult to remove (AND vs OR ???).
        if len(statements) != 1:
            raise InvalidRule('rule must begin with a single top level statement')

        if isinstance(statements[0], capa.engine.Subscope):
            raise InvalidRule('top level statement may not be a subscope')

        return cls(
            name,
            scope,
            build_statements(statements[0], scope),
            d['rule']['meta'],
            s
        )

    @classmethod
    def from_yaml(cls, s):
        return cls.from_dict(yaml.safe_load(s), s)

    @classmethod
    def from_yaml_file(cls, path):
        with open(path, 'rb') as f:
            try:
                return cls.from_yaml(f.read().decode('utf-8'))
            except InvalidRule as e:
                raise InvalidRuleWithPath(path, str(e))

    def to_yaml(self):
        # reformat the yaml document with a common style.
        # this includes:
        #  - ordering the meta elements
        #  - indenting the nested items with two spaces
        #
        # we use the ruamel.yaml parser for this, because it supports roundtripping of documents with comments.

        # order the meta elements in the following preferred order.
        # any custom keys will come after this.
        COMMON_KEYS = ("name", "namespace", "rule-category", "author", "att&ck", "mbc", "examples", "scope")

        yaml = ruamel.yaml.YAML(typ='rt')
        # use block mode, not inline json-like mode
        yaml.default_flow_style = False
        # indent lists by two spaces below their parent
        #
        #     features:
        #       - or:
        #         - mnemonic: aesdec
        #         - mnemonic: vaesdec
        yaml.indent(sequence=2, offset=2)

        definition = yaml.load(self.definition)
        # definition retains a reference to `meta`,
        # so we're updating that in place.
        meta = definition["rule"]["meta"]

        def move_to_end(m, k):
            # ruamel.yaml uses an ordereddict-like structure to track maps (CommentedMap).
            # here we refresh the insertion order of the given key.
            # this will move it to the end of the sequence.
            v = m[k]
            del m[k]
            m[k] = v

        move_to_end(definition["rule"], "meta")
        move_to_end(definition["rule"], "features")

        for key in COMMON_KEYS:
            if key in meta:
                move_to_end(meta, key)

        for key in sorted(meta.keys()):
            if key in COMMON_KEYS:
                continue
            move_to_end(meta, key)

        ostream = six.BytesIO()
        yaml.dump(definition, ostream)
        return ostream.getvalue().decode('utf-8').rstrip("\n")


def get_rules_with_scope(rules, scope):
    '''
    from the given collection of rules, select those with the given scope.

    args:
      rules (List[capa.rules.Rule]):
      scope (str): one of the capa.rules.*_SCOPE constants.

    returns:
      List[capa.rules.Rule]:
    '''
    return list(rule for rule in rules if rule.scope == scope)


def get_rules_and_dependencies(rules, rule_name):
    '''
    from the given collection of rules, select a rule and its dependencies (transitively).

    args:
      rules (List[Rule]):
      rule_name (str):

    yields:
      Rule:
    '''
    rules = {rule.name: rule for rule in rules}
    wanted = set([rule_name])

    def rec(rule):
        wanted.add(rule.name)
        for dep in rule.get_dependencies():
            rec(rules[dep])

    rec(rules[rule_name])

    for rule in rules.values():
        if rule.name in wanted:
            yield rule


def ensure_rules_are_unique(rules):
    seen = set([])
    for rule in rules:
        if rule.name in seen:
            raise InvalidRule('duplicate rule name: ' + rule.name)
        seen.add(rule.name)


def ensure_rule_dependencies_are_met(rules):
    '''
    raise an exception if a rule dependency does not exist.

    raises:
      InvalidRule: if a dependency is not met.
    '''
    rules = {rule.name: rule for rule in rules}
    for rule in rules.values():
        for dep in rule.get_dependencies():
            if dep not in rules:
                raise InvalidRule('rule "%s" depends on missing rule "%s"' % (rule.name, dep))


class RuleSet(object):
    '''
    a ruleset is initialized with a collection of rules, which it verifies and sorts into scopes.
    each set of scoped rules is sorted topologically, which enables rules to match on past rule matches.

    example:

        ruleset = RuleSet([
          Rule(...),
          Rule(...),
          ...
        ])
        capa.engine.match(ruleset.file_rules, ...)
    '''

    def __init__(self, rules):
        super(RuleSet, self).__init__()

        ensure_rules_are_unique(rules)

        rules = self._extract_subscope_rules(rules)

        ensure_rule_dependencies_are_met(rules)

        if len(rules) == 0:
            raise InvalidRuleSet('no rules selected')

        self.file_rules = self._get_rules_for_scope(rules, FILE_SCOPE)
        self.function_rules = self._get_rules_for_scope(rules, FUNCTION_SCOPE)
        self.basic_block_rules = self._get_rules_for_scope(rules, BASIC_BLOCK_SCOPE)
        self.rules = {rule.name: rule for rule in rules}

    def __len__(self):
        return len(self.rules)

    @staticmethod
    def _get_rules_for_scope(rules, scope):
        '''
        given a collection of rules, collect the rules that are needed at the given scope.
        these rules are ordered topologically.

        don't include "lib" rules, unless they are dependencies of other rules.
        '''
        scope_rules = set([])

        # we need to process all rules, not just rules with the given scope.
        # this is because rules with a higher scope, e.g. file scope, may have subscope rules
        #  at lower scope, e.g. function scope.
        # so, we find all dependencies of all rules, and later will filter them down.
        for rule in rules:
            if rule.meta.get('lib', False):
                continue

            scope_rules.update(get_rules_and_dependencies(rules, rule.name))
        return get_rules_with_scope(capa.engine.topologically_order_rules(scope_rules), scope)

    @staticmethod
    def _extract_subscope_rules(rules):
        '''
        process the given sequence of rules.
        for each one, extract any embedded subscope rules into their own rule.
        process these recursively.
        then return a list of the refactored rules.

        note: this operation mutates the rules passed in - they may now have `match` statements
         for the extracted subscope rules.
        '''
        done = []

        # use a queue of rules, because we'll be modifying the list (appending new items) as we go.
        while rules:
            rule = rules.pop(0)
            for subscope_rule in rule.extract_subscope_rules():
                rules.append(subscope_rule)
            done.append(rule)

        return done

    def filter_rules_by_meta(self, tag):
        '''
        return new rule set with rules filtered based on all meta field values, adds all dependency rules
        apply tag-based rule filter assuming that all required rules are loaded
        can be used to specify selected rules vs. providing a rules child directory where capa cannot resolve
        dependencies from unknown paths
        TODO handle circular dependencies?
        TODO support -t=metafield <k>
        '''
        rules = self.rules.values()
        rules_filtered = set([])
        for rule in rules:
            for k, v in rule.meta.items():
                if isinstance(v, str) and tag in v:
                    logger.debug('using rule "%s" and dependencies, found tag in meta.%s: %s', rule.name, k, v)
                    rules_filtered.update(set(capa.rules.get_rules_and_dependencies(rules, rule.name)))
                    break
        return RuleSet(list(rules_filtered))
