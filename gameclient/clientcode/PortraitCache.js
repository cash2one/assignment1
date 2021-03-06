goog.provide('PortraitCache');

// Copyright (c) 2015 SpinPunch. All rights reserved.
// Use of this source code is governed by an MIT-style license that can be
// found in the LICENSE file.

// cache of HTML Image objects representing Facebook portraits, to
// minimize number of instantiated objects.

var PortraitCache = {
    images: {}
};

PortraitCache.get_raw_image = function(url) {
    if(!(url in PortraitCache.images)) {
        var image = new Image();
        image.src = url;
        PortraitCache.images[url] = image;
    }
    return PortraitCache.images[url];
};
