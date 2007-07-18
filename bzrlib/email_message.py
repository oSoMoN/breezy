# Copyright (C) 2007 Canonical Ltd
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

"""A convenience class around email.Message and email.MIMEMultipart."""

from email import (
    Header,
    Message,
    MIMEMultipart,
    MIMEText,
    Utils,
    )

from bzrlib import __version__ as _bzrlib_version
from bzrlib.osutils import safe_unicode
from bzrlib.smtp_connection import SMTPConnection


class EmailMessage:
    """An email message.
    
    The constructor needs an origin address, a destination address or addresses
    and a subject, and accepts a body as well. Add additional parts to the
    message with add_inline_attachment(). Retrieve the entire formatted message
    with as_string().

    Headers can be accessed with get() and msg[], and modified with msg[] =.
    """

    def __init__(self, from_address, to_address, subject, body=None):
        """Create an email message.

        :param from_address: The origin address, to be put on the From header.
        :param to_address: The destination address of the message, to be put in
            the To header. Can also be a list of addresses.
        :param subject: The subject of the message.
        :param body: If given, the body of the message.

        All four parameters can be unicode strings or byte strings, but for the
        addresses and subject byte strings must be encoded in UTF-8. For the
        body any byte string will be accepted; if it's not ASCII or UTF-8,
        it'll be sent with charset=8-bit.
        """
        self._headers = {}
        self._body = body
        self._parts = []
        self._msgobj = None

        if isinstance(to_address, basestring):
            to_address = [ to_address ]

        to_addresses = []

        for addr in to_address:
            to_addresses.append(self.address_to_encoded_header(addr))

        self._headers['To'] = ', '.join(to_addresses)
        self._headers['From'] = self.address_to_encoded_header(from_address)
        self._headers['Subject'] = Header.Header(safe_unicode(subject))
        self._headers['User-Agent'] = 'Bazaar (%s)' % _bzrlib_version

    def add_inline_attachment(self, body, filename=None, mime_subtype='plain'):
        """Add an inline attachment to the message.

        :param body: A text to attach. Can be an unicode string or a byte
            string, and it'll be sent as ascii, utf-8, or 8-bit, in that
            preferred order.
        :param filename: The name for the attachment. This will give a default
            name for email programs to save the attachment.
        :param mime_subtype: MIME subtype of the attachment (eg. 'plain' for
            text/plain [default]).

        The attachment body will be displayed inline, so do not use this
        function to attach binary attachments.
        """
        # add_inline_attachment() has been called, so the message will be a
        # MIMEMultipart; add the provided body, if any, as the first attachment
        if self._body is not None:
            self._parts.append((self._body, None, 'plain'))
            self._body = None

        self._parts.append((body, filename, mime_subtype))
        self._msgobj = None

    def as_string(self, boundary=None):
        """Return the entire formatted message as a string.
        
        :param boundary: The boundary to use between MIME parts, if applicable.
            Used for tests.
        """
        if self._msgobj is not None:
            return self._msgobj.as_string()

        if not self._parts:
            self._msgobj = Message.Message()
            if self._body is not None:
                string, encoding = self.string_with_encoding(self._body)
                self._msgobj.set_payload(string, encoding)
        else:
            self._msgobj = MIMEMultipart.MIMEMultipart()

            if boundary is not None:
                self._msgobj.set_boundary(boundary)

            for body, filename, mime_subtype in self._parts:
                string, encoding = self.string_with_encoding(body)
                payload = MIMEText.MIMEText(string, mime_subtype, encoding)

                if filename is not None:
                    content_type = payload['Content-Type']
                    content_type += '; name="%s"' % filename
                    payload.replace_header('Content-Type', content_type)

                payload['Content-Disposition'] = 'inline'
                self._msgobj.attach(payload)

        # sort headers here to ease testing
        for header, value in sorted(self._headers.items()):
            self._msgobj[header] = value

        return self._msgobj.as_string()

    __str__ = as_string

    def get(self, header, failobj=None):
        """Get a header from the message, returning failobj if not present."""
        return self._headers.get(header, failobj)

    def __getitem__(self, header):
        """Get a header from the message, returning None if not present.
        
        This method does intentionally not raise KeyError to mimic the behavior
        of __getitem__ in email.Message.
        """
        return self._headers.get(header, None)

    def __setitem__(self, header, value):
        return self._headers.__setitem__(header, value)

    @staticmethod
    def send(config, from_address, to_address, subject, body, attachment=None,
            attachment_filename=None, attachment_mime_subtype='plain'):
        """Create an email message and send it with SMTPConnection.

        :param config: config object to pass to SMTPConnection constructor.

        See EmailMessage.__init__() and EmailMessage.add_inline_attachment()
        for an explanation of the rest of parameters.
        """
        msg = EmailMessage(from_address, to_address, subject, body)
        if attachment is not None:
            msg.add_inline_attachment(attachment, attachment_filename,
                    attachment_mime_subtype)
        SMTPConnection(config).send_email(msg)

    @staticmethod
    def address_to_encoded_header(address):
        """RFC2047-encode an address if necessary.

        :param address: An unicode string, or UTF-8 byte string.
        :return: A possibly RFC2047-encoded string.
        """
        # Can't call Header over all the address, because that encodes both the
        # name and the email address, which is not permitted by RFCs.
        user, email = Utils.parseaddr(address)
        if not user:
            return email
        else:
            return Utils.formataddr((str(Header.Header(safe_unicode(user))),
                email))

    @staticmethod
    def string_with_encoding(string):
        """Return a str object together with an encoding.

        :param string: A str or unicode object.
        :return: A tuple (str, encoding), where encoding is one of 'ascii',
            'utf-8', or '8-bit', in that preferred order.
        """
        # Python's email module base64-encodes the body whenever the charset is
        # not explicitly set to ascii. Because of this, and because we want to
        # avoid base64 when it's not necessary in order to be most compatible
        # with the capabilities of the receiving side, we check with encode()
        # and decode() whether the body is actually ascii-only.
        if isinstance(string, unicode):
            try:
                return (string.encode('ascii'), 'ascii')
            except UnicodeEncodeError:
                return (string.encode('utf-8'), 'utf-8')
        else:
            try:
                string.decode('ascii')
                return (string, 'ascii')
            except UnicodeDecodeError:
                try:
                    string.decode('utf-8')
                    return (string, 'utf-8')
                except UnicodeDecodeError:
                    return (string, '8-bit')
