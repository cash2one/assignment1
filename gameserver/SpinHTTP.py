#!/usr/bin/env python

# Copyright (c) 2015 SpinPunch. All rights reserved.
# Use of this source code is governed by an MIT-style license that can be
# found in the LICENSE file.

# HTTP utilities

import base64

# wrap/unwrap Unicode text strings for safe transmission across the AJAX connection
# mirrors gameclient/clientcode/SPHTTP.js

def unwrap_string(input):
    return unicode(base64.b64decode(str(input)).decode('utf-8'))

def wrap_string(input):
    return base64.b64encode(input.encode('utf-8'))

# set cross-site allow headers on Twisted HTTP requests
def _set_access_control_headers(request, origin, max_age):
    if origin:
        request.setHeader('Access-Control-Allow-Origin', origin)
    request.setHeader('Access-Control-Allow-Credentials', 'true')
    request.setHeader('Access-Control-Allow-Methods', 'POST, GET, HEAD, OPTIONS')
    request.setHeader('Access-Control-Allow-Headers', 'X-Requested-With')
    request.setHeader('Access-Control-Max-Age', str(max_age))

# get a raw HTTP header from Twisted request object
def get_twisted_header(request, x):
    temp = request.requestHeaders.getRawHeaders(x)
    if temp and len(temp) > 0:
        return str(temp[0])
    else:
        return ''
def set_twisted_header(request, x, val):
    request.requestHeaders.setRawHeaders(x, [val])

def set_access_control_headers(request):
    if request.requestHeaders.hasHeader('origin'):
        origin = get_twisted_header(request, 'origin')
    elif 'spin_origin' in request.args:
        origin = request.args['spin_origin'][-1]
    else:
        origin = '*'
    _set_access_control_headers(request, origin, 7*24*60*60)

def set_access_control_headers_for_cdn(request, max_age):
    # ensure that we ONLY attach a non-wildcard origin if it's in the query string
    if 'spin_origin' in request.args:
        origin = request.args['spin_origin'][-1]
    else:
        origin = '*'
    _set_access_control_headers(request, origin, max_age)

