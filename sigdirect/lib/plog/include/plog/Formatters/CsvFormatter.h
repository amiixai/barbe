/*******************************************************************************
 * Copyright (C) 2020 Mohammad Motallebi
 * 
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 * 
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 * 
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 ******************************************************************************/
#pragma once
#include <plog/Record.h>
#include <plog/Util.h>
#include <iomanip>

namespace plog
{
    template<bool useUtcTime>
    class CsvFormatterImpl
    {
    public:
        static util::nstring header()
        {
            return PLOG_NSTR("Date;Time;Severity;TID;This;Function;Message\n");
        }

        static util::nstring format(const Record& record)
        {
            tm t;
            useUtcTime ? util::gmtime_s(&t, &record.getTime().time) : util::localtime_s(&t, &record.getTime().time);

            util::nostringstream ss;
            ss << t.tm_year + 1900 << PLOG_NSTR("/") << std::setfill(PLOG_NSTR('0')) << std::setw(2) << t.tm_mon + 1 << PLOG_NSTR("/") << std::setfill(PLOG_NSTR('0')) << std::setw(2) << t.tm_mday << PLOG_NSTR(";");
            ss << std::setfill(PLOG_NSTR('0')) << std::setw(2) << t.tm_hour << PLOG_NSTR(":") << std::setfill(PLOG_NSTR('0')) << std::setw(2) << t.tm_min << PLOG_NSTR(":") << std::setfill(PLOG_NSTR('0')) << std::setw(2) << t.tm_sec << PLOG_NSTR(".") << std::setfill(PLOG_NSTR('0')) << std::setw(3) << static_cast<int> (record.getTime().millitm) << PLOG_NSTR(";");
            ss << severityToString(record.getSeverity()) << PLOG_NSTR(";");
            ss << record.getTid() << PLOG_NSTR(";");
            ss << record.getObject() << PLOG_NSTR(";");
            ss << record.getFunc() << PLOG_NSTR("@") << record.getLine() << PLOG_NSTR(";");

            util::nstring message = record.getMessage();

            if (message.size() > kMaxMessageSize)
            {
                message.resize(kMaxMessageSize);
                message.append(PLOG_NSTR("..."));
            }

            util::nistringstream split(message);
            util::nstring token;

            while (!split.eof())
            {
                std::getline(split, token, PLOG_NSTR('"'));
                ss << PLOG_NSTR("\"") << token << PLOG_NSTR("\"");
            }

            ss << PLOG_NSTR("\n");

            return ss.str();
        }

        static const size_t kMaxMessageSize = 32000;
    };

    class CsvFormatter : public CsvFormatterImpl<false> {};
    class CsvFormatterUtcTime : public CsvFormatterImpl<true> {};
}
