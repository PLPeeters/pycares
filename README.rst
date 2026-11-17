pycares: Python interface for c-ares
====================================

.. image:: https://badge.fury.io/py/pycares.png
    :target: https://pypi.org/project/pycares/

.. image:: https://github.com/saghul/pycares/workflows/Test/badge.svg
    :target: https://github.com/saghul/pycares/actions

pycares is a Python module which provides an interface to c-ares.
`c-ares <https://c-ares.org>`_ is a C library that performs
DNS requests and name resolutions asynchronously.


Documentation
-------------

https://pycares.readthedocs.io/latest/


Migrating to version 5
----------------------

Version 5 introduces a number of breaking changes, please check the
migration guide here: https://pycares.readthedocs.io/latest/migration.html


Build Requirements
------------------

To build pycares from source, you need:

- Python development headers
- CMake >= 3.5 (https://cmake.org/)
- A C compiler

Installing CMake:

- **Ubuntu/Debian**: ``apt-get install cmake``
- **RHEL/CentOS/Fedora**: ``dnf install cmake`` or ``yum install cmake``
- **macOS**: ``brew install cmake`` or download from cmake.org
- **Windows**: Download from https://cmake.org/download/
- **FreeBSD**: ``pkg install cmake``

pycares currently bundles c-ares as a submodule for ease of building. Using the system
provided c-ares is possible if the ``PYCARES_USE_SYSTEM_LIB`` environment variable is
set to ``1`` when building.


Installation
------------

GNU/Linux, macOS, Windows, others:

::

    pip install pycares

FreeBSD:

::

    cd /usr/ports/dns/py-pycares && make install


IDNA 2008 support
^^^^^^^^^^^^^^^^^

If the ``idna`` package is installed, pycares will support IDNA 2008 encoding otherwise the builtin idna codec will be used,
which provides IDNA 2003 support.

You can force this at installation time as follows:

::

   pip install pycares[idna]


Running the test suite
----------------------

From the top level directory, run: ``python -m unittest -v``

NOTE: Running the tests requires internet access and are somewhat environment sensitive because real DNS queries
are made, there is no mocking. If you observe a failure that the CI cannot reproduce, please try to setup an
environment as close as the current CI.


Using it from the cli, a la dig
-------------------------------

This module can be used directly from the command line in a similar fashion to dig (limited, of course):

::

   $ python -m pycares google.com
   ;; QUESTION SECTION:
   ;google.com			IN	A

   ;; ANSWER SECTION:
   google.com		300	IN	A	172.217.17.142

   $ python -m pycares mx google.com
   ;; QUESTION SECTION:
   ;google.com			IN	MX

   ;; ANSWER SECTION:
   google.com		600	IN	MX	50 alt4.aspmx.l.google.com
   google.com		600	IN	MX	10 aspmx.l.google.com
   google.com		600	IN	MX	40 alt3.aspmx.l.google.com
   google.com		600	IN	MX	20 alt1.aspmx.l.google.com
   google.com		600	IN	MX	30 alt2.aspmx.l.google.com


Individual query cancellation
-----------------------------

``Channel.query_cancellable()``, ``search_cancellable()``, and
``getaddrinfo_cancellable()`` return an ``Operation`` with a ``cancel()`` method.
Cancellation affects that lookup only and delivers one terminal callback with
``ARES_ECANCELLED`` when cancellation wins. Existing callback methods still
return ``None``. See ``docs/channel.rst`` for lifetime and transport details.

The bundled c-ares includes the required native cancellation extension.
``PYCARES_USE_SYSTEM_LIB=1`` requires headers and a library implementing that
extension; an unmodified c-ares 1.34.9 does not suffice. Unsupported system
libraries fail the build with an actionable error. Unset that environment
variable to use the bundled implementation. Cancellation is never emulated by
canceling every query on a channel.


Author
------

Saúl Ibarra Corretgé <s@saghul.net>


License
-------

Unless stated otherwise on-file pycares uses the MIT license, check LICENSE file.


Supported Python versions
-------------------------

Python >= 3.11 are supported. Both CPython and PyPy are supported.


Contributing
------------

If you'd like to contribute, fork the project, make a patch and send a pull
request. Have a look at the surrounding code and please, make yours look
alike.
