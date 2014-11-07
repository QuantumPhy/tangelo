import tangelo
import tangelo.util

# Useful aliases for this service's necessary persistent data.
store = tangelo.persistent_store()
streams = store["streams"] = {}
modules = store["modules"] = tangelo.util.ModuleCache()

@tangelo.restful
def get(key=None):
    return get_streams() if key is None else get_stream_info(key)

@tangelo.restful
def post(*pathcomp, **kwargs):
    if len(pathcomp) == 0:
        # TODO: raise error condition
        pass

    action = pathcomp[0]
    args = pathcomp[1:]

    if action == "start":
        if len(args) == 0:
            tangelo.http_status(400, "Path To Service Required")
            return {"error": "No service path was specified"}

        return stream_start("/" + "/".join(args), kwargs)
    elif action == "next":
        if len(args) != 1:
            tangelo.http_status(400, "Stream Key Required")
            return {"error": "No stream key was specified"}

        return stream_next(args[0])
    else:
        tangelo.http_status(400, "Illegal POST action")
        return {"error": "Illegal POST action '%s'" % (action)}

@tangelo.restful
def delete(key=None):
    if key is None:
        tangelo.http_status(400, "Stream Key Required")
        return {"error": "No stream key was specified"}
    elif key not in streams:
        tangelo.http_status(404, "No Such Stream Key")
        return {"error": "Key '%s' does not correspond to an active stream" % (key)}
    else:
        del streams[key]
        return {"key": key}
    
def get_streams():
    return streams.keys()

def get_stream_info(key):
    tangelo.http_status(501)
    return {"error": "stream info method currently unimplemented"}

def stream_start(url, kwargs):
    directive = tangelo.tool.analyze_url(url)

    if "target" not in directive or directive["target"].get("type") != "service":
        tangelo.log("STREAM", json.dumps(directive, indent=4))
        tangelo.http_status(500, "Error Opening Streaming Service")
        return {"error": "could not open streaming service"}
    else:
        # Extract the path to the service and the list of positional
        # arguments.
        module_path = directive["target"]["path"]
        pargs = directive["target"]["pargs"]

        # Get the service module.
        try:
            service = modules.get(module_path)
        except tangelo.HTTPStatusCode as e:
            tangelo.http_status(e.code)
            return {"error": e.msg or ""}
        else:
            # Check for a "stream" function inside the module.
            if "stream" not in dir(service):
                tangelo.http_status(400, "Non-Streaming Service")
                return {"error": "The requested streaming service does not implement a 'stream()' function"}
            else:
                # Call the stream function and capture its result.
                try:
                    stream = service.stream(*pargs, **kwargs)
                except Exception as e:
                    bt = traceback.format_exc()

                    tangelo.log("Caught exception while executing service %s" %
                                (tangelo.request_path()), "SERVICE")
                    tangelo.log(bt, "SERVICE")

                    tangelo.http_status(500, "Streaming Service Raised Exception")
                    return {"error": "Caught exception during streaming service execution: %s" % (str(bt))}
                else:
                    # Generate a key corresponding to this object.
                    key = tangelo.util.generate_key(streams)

                    # Log the object in the streaming table.
                    streams[key] = stream

                    # Create an object describing the logging of the generator object.
                    return {"key": key}

def stream_next(key):
    if key not in streams:
        tangelo.http_status(404, "No Such Key")
        return {"error": "Key '%s' does not correspond to an active stream" % (key)}
    else:
        # Grab the stream in preparation for running it.
        stream = streams[key]

        # Attempt to run the stream via its next() method - if this
        # yields a result, then continue; if the next() method raises
        # StopIteration, then there are no more results to retrieve; if
        # any other exception is raised, this is treated as an error.
        try:
            return stream.next()
        except StopIteration:
            del streams[key]

            tangelo.http_status(204, "Stream Finished")
            return "OK"
        except:
            del streams[key]
            tangelo.http_status(500, "Exception Raised By Streaming Service")
            return {"error": "Caught exception while executing stream service keyed by %s:<br><pre>%s</pre>" % (key, traceback.format_exc())}

