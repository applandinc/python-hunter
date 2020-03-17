import inspect
import os
import sys
import json

from re import sub

from anytree import AnyNode, Node, Resolver
from anytree.resolver import Resolver, ChildResolverError
from anytree.exporter import DictExporter, JsonExporter

from benedict import benedict

import inflection

from .actions import ColorStreamAction

class AppmapPrinter(ColorStreamAction):
    """
    An action that writes appmap entries.

    TODO: update help for args
    Args:
        stream (file-like): Stream to write to. Default: ``sys.stderr``.
        filename_alignment (int): Default size for the filename column (files are right-aligned). Default: ``40``.
        force_colors (bool): Force coloring. Default: ``False``.
        repr_limit (bool): Limit length of ``repr()`` output. Default: ``512``.
        repr_func (string or callable): Function to use instead of ``repr``.
            If string must be one of 'repr' or 'safe_repr'. Default: ``'safe_repr'``.
    """
    current_frame = None
    
    def __init__(self, *args, **kwargs):
        self.test_item = kwargs.pop('pytest_item')
        super(AppmapPrinter, self).__init__(*args, **kwargs)
        self.event_id = 1

        self.classmap = Node('root', type='none')
        self.functions = benedict()
        
        self.appmap = None
        self.setup()

    def __del__(self):
        self.teardown()
        
    def __call__(self, event):
        """
        Handle event and write an entry to the appmap.
        """

        if event.kind not in ('call', 'return'):
            return
        
        if event.kind == 'call':
            evt = self.inspect_call(event)
        elif event.kind == 'return':
            evt = self.inspect_return(event)
        else:
            raise RuntimeError(f"unhandled event kind {event.kind}")
        
        if evt is not None:
            self.appmap.write(',' if self.event_id > 1 else '')
            evt.pop('fq_class')
            self.appmap.write(json.dumps(evt))
            self.event_id += 1

    def setup(self):
        output_dir = 'tmp/appmap/pytest'
        os.makedirs(output_dir, exist_ok=True)
        fname = self.test_item.name

        # replace unwanted with '_'
        fname = sub('[^a-z0-9\-_]+', '_', fname)
        # replace duplicate '_' with '_'
        fname = sub('_{2,}', '_', fname)
        # remove leading and trailing '_'
        fname = sub('^_|^$', '', fname)

        self.appmap = open(os.path.join(output_dir, f"{fname}.appmap.json"), "w")
        self.appmap.write('{"version": "1.2"')
        self.appmap.write(',"events": [')
        return self

    def humanize(self, str):
        return inflection.humanize(sub('^test_', '', str.replace('.', ' ')))
        
    def teardown(self):
        if not self.__dict__.get('appmap', None):
            sys.stderr.write('no appmap?\n')
            return
        
        self.appmap.write('],"classMap":[\n')

        # For each class, Convert the dict of locations to its
        # children, a list of appmap functions.
        def cleanup_functions(attrs):
            ret = {k:v for (k,v) in attrs if k != 'fq_class'}
            if ret['type'] == 'class':
                functions = ret.pop('functions')
                ret['children'] = list(functions.values())
            return ret

        exporter = JsonExporter(dictexporter=DictExporter(attriter=cleanup_functions))

        comma = ''
        for root in self.classmap.children:
            self.appmap.write(comma)
            self.appmap.write(
                exporter.export(root)
            )
            comma = ','
        self.appmap.write(']')

        feature = self.humanize(self.test_item.module.__name__)
        metadata = {
            "name": self.humanize(self.test_item.name),
            "app": 'Tamr Client',
            "feature_group": feature,
            "feature": feature,
            "frameworks": [
                {
                    "name": "pytest",
                    "version": "5.3.5"
                }
            ]
        }
        self.appmap.write(',"metadata":' + json.dumps(metadata) + '}\n')
        self.appmap.close()

    def display_string(self, value):
        try:
            try:
                return str(value)
            except:
                return repr(value)
        except Exception as exc:
            return f"Failed rendering value as a string, {exc}"
        
            
    def inspect_event(self, event):
        fo = event.function_object
        if fo is None:
            # sys.stderr.write(f"inspect_event, ignoring event without function_object, module: {event.module} frame: {event.frame}\n")
            return None

        # sys.stderr.write(f"{event.kind}: {fo.__qualname__} {event.module} {event.function} {event.filename}:{event.lineno}\n")

        # XXX ignore bare functions for now
        if not '.' in fo.__qualname__:
            # sys.stderr.write(f"inspect_event, ignoring bare function {event.function}, frame: {event.frame}\n")
            return None
        cls,_,method = fo.__qualname__.rpartition('.')

        entry = {
            "fq_class": f"{fo.__module__}.{cls}",
            "id": self.event_id,
            "event": event.kind,
             "defined_class": cls,
            "method_id": method,
            "path": event.filename,
            "lineno" : event.code.co_firstlineno,
        }
            
        self.add_to_classmap(entry)

        return entry
    
    def inspect_call(self, event):
        entry = self.inspect_event(event)
        if entry is None:
            return None
        
        # sanity check
        if '' in entry.values():
            raise RuntimeError(f"internal error, missing values: {entry}")
        
        receiver = None
        instance_class = None             
        params = list()
        frame = event.frame
        for i, p in enumerate(frame.f_code.co_varnames[:frame.f_code.co_argcount]):
            param_class = frame.f_locals[p].__class__
            param = {
                "name": p,
                "class": f"{param_class.__module__}.{param_class.__qualname__}"
            }
            param['kind'] = 'req' # XXX pretend all are required for now

            # XXX This is wrong for classmethods
            if i == 0 and p == 'self':
                receiver = param
                instance_class = frame.f_locals[p].__class__
                continue

            param['value'] = self.display_string(frame.f_locals[p])[:100]
            params.append(param)

        event.frame.f_locals['_appmap_event_id'] = self.event_id

        entry = {
            **entry,
            "static": True,
            "parameters" : params,
            # XXX fake values
            "thread_id" : 1
        }
        if receiver:
            entry['receiver'] = receiver
            entry['static'] = False

        self.add_function(entry['fq_class'], entry, instance_class)

        return entry

    def inspect_return(self, event):
        entry = self.inspect_event(event)
        if entry is None:
            return None
        
        if not '_appmap_event_id' in event.frame.f_locals:
#            sys.stderr.write(f"WARNING, inspect_return ignoring event: no event id found in {event.frame.f_back.f_locals}\n")
#            sys.stderr.write(f"WARNING, inspect_return ignoring event: no event id found\n")
#            sys.stderr.write(f"  entry: {entry}\n")
            return None

        parent_id = event.frame.f_locals.pop('_appmap_event_id')

        ret = {
            **entry,
            "parent_id": parent_id,
        }
        if event.arg:
            ret_class = event.arg.__class__
            ret["return_value"] = {
                "class": f"{ret_class.__module__}.{ret_class.__qualname__}",
                "value": self.display_string(event.arg)
            }
            
        return ret
    
    def add_to_classmap(self, entry):
        fq_class = entry['fq_class']
        if fq_class in self.functions:
            return

        r = Resolver("name")
        mods,_,cls = fq_class.rpartition('.')
        map_entry = self.classmap
        for mod in mods.split('.'):
            try:
                map_entry = r.get(map_entry, mod)
            except ChildResolverError:
                map_entry = Node(mod, parent=map_entry, type='package')
        functions = {}
        map_entry = Node(cls, parent=map_entry, type='class', functions=functions)
        self.functions[fq_class] = functions
        
    def add_function(self, fq_class, entry, instance_class):
        location = f"{entry['path']}:{entry['lineno']}"
        if location in self.functions[fq_class]:
            return
        
        name = entry['method_id']
        function_entry = {
            "type": "function",
            "name": name,
            "location": location,
            "static": False
        }
        labels = []
        if name == '__init__':
            labels.append('ctor')
        elif name == '__setattr__':
            labels.append('setter')
        elif name == '__getattr__':
            labels.append('getter')

        if instance_class:
            function_labels = self.label_function(instance_class, name)
            if len(function_labels) > 0:
                labels.append(function_labels)
                
        if len(labels) > 0:
            function_entry['labels'] = labels

        self.functions[fq_class][location] = function_entry

    def label_function(self, cls, name):
        labels = []
        if name in cls.__dict__:
            attr = cls.__dict__[name]
            if isinstance(attr, property) and attr.fget:
                labels.append(['getter'])
        return labels
