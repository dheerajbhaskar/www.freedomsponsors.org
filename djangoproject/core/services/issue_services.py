from urlparse import urlparse
from decimal import Decimal
from django.db.models import Q
from core.services.mail_services import *
from core.services import watch_services
from core.templatetags.markdown import strip_markdown
from core.utils.frespo_utils import get_or_none
from core.models import Issue, Project, Offer, Solution, IssueComment, OfferComment
from core.utils.trackers_adapter import fetchIssueInfo
import logging

logger = logging.getLogger(__name__)

__author__ = 'tony'


def search_issues(project_id=None, project_name=None, search_terms='', is_sponsored=None,
                  sortby=None, asc=True):
    if search_terms:
        issue = _find_by_tracker_url(search_terms)
        if issue:
            return issue

    issues = Issue.objects.filter(Q(is_feedback=False) | Q(offer__isnull=False)).distinct()
    if is_sponsored != None:
        issues = issues.filter(is_sponsored=is_sponsored)
    if project_id:
        issues = issues.filter(project__id=project_id)
    elif project_name:
        issues = issues.filter(project__name__icontains=project_name)
    if search_terms:
        issues = issues.filter(Q(title__icontains=search_terms) | Q(trackerURL__icontains=search_terms))
    sorts = _get_sorts(sortby, asc)
    issues = issues.order_by(*sorts)
    return issues


def _get_sorts(sortby, asc):
    if not sortby:
        return ['-updatedDate']
    prefix = '-' if not asc else ''
    if sortby == 'project':
        return [prefix + 'project__name']
    if sortby == 'key':
        return [prefix + 'key']
    if sortby == 'title':
        return [prefix + 'title']
    if sortby == 'createdby':
        return [prefix + 'createdByUser__username']
    if sortby == 'offers':
        return [prefix + 'total_open_offers_btc', prefix + 'total_open_offers_usd']
    if sortby == 'solutions':
        return [prefix + 'count_solutions_done', prefix + 'count_solutions_in_progress']
    if sortby == 'created':
        return [prefix + 'creationDate']
    return ['-updatedDate']


def _find_by_tracker_url(url):
    url = strip_protocol(url)
    return get_or_none(Issue, trackerURL_noprotocol__iexact=url)


def sponsor_new_issue(dict, user):
    offer = _buildOfferFromDictionary(dict, user)
    if offer.issue.project:
        offer.issue.project.save()
        offer.issue.project = offer.issue.project
    if not offer.issue.id:
        offer.issue.save()
        offer.issue = offer.issue
    offer.save()
    offer.issue.update_redundant_fields()
    msg = "offer: " + str(offer.price) + "\n<br>" +\
          "issue key: " + offer.issue.key + "\n<br>" +\
          "issue title: " + offer.issue.title + "\n<br>"
    if offer.issue.project:
        msg += "project : " + offer.issue.project.name + "\n<br>" +\
               "project.trackerURL: " + offer.issue.project.trackerURL + "\n<br>"
    notify_admin("INFO: New issue sponsored", msg)
    return offer


def kickstart_new_issue(dict, user):
    issue = _buildIssueFromDictionary(dict, user)
    issue.is_sponsored = False
    if(issue.project):
        issue.project.save()
        issue.project = issue.project
    issue.save()
    msg = "issue key: " + issue.key + "\n<br>" +\
          "issue title: " + issue.title + "\n<br>"
    if(issue.project):
        msg += "project : " + issue.project.name + "\n<br>" +\
               "project.trackerURL: " + issue.project.trackerURL + "\n<br>"
    notify_admin("INFO: New issue proposed", msg)
    return issue


def sponsor_existing_issue(issue_id, dict, user):
    issue = Issue.objects.get(pk=issue_id)
    issue.touch()
    _throwIfAlreadySponsoring(issue, user)
    offer = _buildOfferFromDictionary_and_issue(dict, user, issue);
    offer.save()
    issue.update_redundant_fields()
    watches = watch_services.find_issue_and_project_watches(issue)
    notifyWatchers_offeradded(offer, watches)
    msg = "offer: " + str(offer.price) + "\n<br>" +\
          "issue key: " + offer.issue.key + "\n<br>" +\
          "issue title: " + offer.issue.title + "\n<br>"
    if(offer.issue.project):
        msg += "project : " + offer.issue.project.name + "\n<br>" +\
               "project.trackerURL: " + offer.issue.project.trackerURL + "\n<br>"
    notify_admin("INFO: Existing issue sponsored", msg)
    return offer


def change_existing_issue(issue_id, issuedict, logo, user):
    issue = Issue.objects.get(pk=issue_id)
    _throwIfNotIssueCreator(issue, user)
    issue.changeIssue(issuedict, logo)
    return issue


def change_existing_offer(offer_id, offerdict, user):
    offer = Offer.objects.get(pk=offer_id)
    _throwIfNotOfferOwner(offer, user)
    old_offer = offer.clone()
    offer.changeOffer(offerdict)
    offer.issue.update_redundant_fields()
    watches = watch_services.find_issue_and_project_watches(offer.issue)
    notifyWatchers_offerchanged(old_offer, offer, watches)
    return offer


def add_solution_to_existing_issue(issue_id, comment_content, accepting_payments, user):
    issue = Issue.objects.get(pk=issue_id)
    solution = get_or_none(Solution, issue=issue, programmer=user)
    if(solution):
        _throwIfSolutionInProgress(solution, user, 'add solution')
        solution.reopen(accepting_payments)
    else:
        solution = Solution.newSolution(issue, user, accepting_payments)
    solution.save()
    issue.update_redundant_fields();
    comment = None
    if(comment_content):
        comment = IssueComment.newComment(issue, user, comment_content)
        comment.save()
    watches = watch_services.find_issue_and_project_watches(solution.issue)
    notifyWatchers_workbegun(solution, comment, watches)
    if(accepting_payments):
        notifyWatchers_acceptingpayments(solution, watches)
    return solution, comment


def abort_existing_solution(solution_id, comment_content, user):
    solution = Solution.objects.get(pk=solution_id)
    _throwIfNotSolutionOwner(solution, user)
    _throwIfSolutionNotInProgress(solution, user, 'abort solution')
    solution.abort()
    solution.issue.update_redundant_fields();
    comment = None
    if(comment_content):
        comment = IssueComment.newComment(solution.issue, user, comment_content)
        comment.save()
    watches = watch_services.find_issue_and_project_watches(solution.issue)
    notifyWatchers_workstopped(solution, comment, watches)

    return solution, comment


def revoke_existing_offer(offer_id, comment_content, user):
    offer = Offer.objects.get(pk=offer_id)
    _throwIfNotOfferOwner(offer, user)
    _throwIfOfferNotOpen(offer, user, 'revoke offer')
    offer.revoke()
    offer.issue.update_redundant_fields()
    comment = None
    if(comment_content):
        comment = IssueComment.newComment(offer.issue, user, comment_content)
        comment.save()
    watches = watch_services.find_issue_and_project_watches(offer.issue)
    notifyWatchers_offerrevoked(offer, comment, watches)
    return offer, comment


def resolve_existing_solution(solution_id, comment_content, user):
    solution = Solution.objects.get(pk=solution_id)
    _throwIfNotSolutionOwner(solution, user)
    _throwIfSolutionNotInProgress(solution, user, 'resolve solution')
    solution.resolve()
    solution.issue.update_redundant_fields()
    comment = None
    if(comment_content):
        comment = IssueComment.newComment(solution.issue, user, comment_content)
        comment.save()
    watches = watch_services.find_issue_and_project_watches(solution.issue)
    notifyWatchers_workdone(solution, comment, watches)
    return solution, comment


def process_issue_url(trackerURL, user):
    result = {"urlValidationError": validateIssueURL(trackerURL)}
    if not result["urlValidationError"]:
        issues = Issue.objects.filter(trackerURL__iexact=trackerURL)
        issue_already_exists = issues.count() >= 1
        if issues.count() > 1:
            logger.warning("Database inconsistency: more than one issue found with url = %s"%trackerURL)
        if issue_already_exists:
            result["issue"] = {"id": issues[0].id}
            return result
        else:
            issueInfo = fetchIssueInfo(trackerURL)
            _append_project_id_and_update_db_if_needed(issueInfo, trackerURL, user)
            result["issueInfo"] = issueInfo.__dict__
    return result


def to_card_dict(issues):
    result = []
    for issue in issues:
        four_sponsors = []
        dic = {'id': issue.id,
               'title': issue.title,
               'status': issue.get_status(),
               'sponsor_status': 'SPONSORED' if issue.is_sponsored else 'PROPOSED',
               'project_link': '#',
               'issue_link': issue.get_view_link(),
               'description': strip_markdown(issue.description),
               'total_paid_offers_usd': str(issue.total_paid_offers_usd),
               'total_paid_offers_btc': str(issue.total_paid_offers_btc),
               'total_open_offers_usd': str(issue.total_open_offers_usd),
               'total_open_offers_btc': str(issue.total_open_offers_btc),
               'four_sponsors': four_sponsors,
               'moresponsors': max(issue.getOffers().count() - 4, 0),
               'image_link': issue.get_card_image(),
               'viewcount': -1,
               'commentcount': issue.getComments().count()}
        if issue.project:
            dic['project_link'] = issue.project.get_view_link()
            dic['project_name'] = issue.project.name
        for offer in issue.getOffers()[0:4]:
            sponsor = {
                'image_link': offer.sponsor.gravatar_url_medium(),
                'username': offer.sponsor.username,
                'sponsor_link': offer.sponsor.get_view_link(),
            }
            four_sponsors.append(sponsor)
        result.append(dic)
    return result


def list_issues_dict(filters=None):
    if not filters:
        filters = {}
    issues = search_issues(search_terms=filters.get('q', ''))
    issues_dict = to_card_dict(issues)
    return issues_dict


def _buildOfferFromDictionary(dict, user):
    issue = _buildIssueFromDictionary(dict, user)
    return _buildOfferFromDictionary_and_issue(dict, user, issue);


def _buildIssueFromDictionary(dict, user):
    check_noProject = dict.has_key('noProject')
    issue_trackerURL = dict['trackerURL']
    issue_projectId = dict['project_id']
    issue_projectName = dict.get('project_name', '')
    check_createProject = dict.has_key('createProject')
    newProject_name = dict.get('newProjectName', '')
    newProject_homeURL = dict.get('newProjectHomeURL', '')
    newProject_trackerURL = dict.get('newProjectTrackerURL', '')
    issue_key = dict.get('key', '');
    issue_title = dict.get('title', '');
    issue_description = dict.get('description', '');
    _throwIfIssueExists(issue_trackerURL, user)
    issue = None
    if(check_noProject):
        if(not issue_title or not issue_description):
            raise BaseException('title and description are required')

        issue = Issue.newIssueOrphan(issue_title, issue_description, user)
    else:
        project = None
        if(check_createProject):
            if(not newProject_name or not newProject_homeURL or not newProject_trackerURL):
                raise BaseException('all parameters for new project are required')

            projectHomeURLValidationError = validateURL(newProject_homeURL)
            if(projectHomeURLValidationError):
                raise BaseException(
                    'invalid project URL (' + newProject_homeURL + ') - ' + projectHomeURLValidationError)

            projectTrackerURLValidationError = validateURL(newProject_trackerURL)
            if(projectTrackerURLValidationError):
                raise BaseException(
                    'invalid project tracker URL (' + newProject_trackerURL + ') - ' + projectTrackerURLValidationError)

            project = Project.newProject(newProject_name, user, newProject_homeURL, newProject_trackerURL)
        else:
            project = Project.objects.get(pk=int(issue_projectId))
            if(newProject_homeURL != project.homeURL):
                project.homeURL = newProject_homeURL

        if(not issue_key or not issue_title):
            raise BaseException('key and title are required')

        issueURLValidationError = validateIssueURL(issue_trackerURL)
        if(issueURLValidationError):
            raise BaseException('invalid issue URL (' + issue_trackerURL + ') - ' + issueURLValidationError)

        issue = Issue.newIssue(project, issue_key, issue_title, issue_description, user, issue_trackerURL)
    return issue


def _throwIfIssueExists(trackerURL, user):
    if(trackerURL):
        issues = Issue.objects.filter(trackerURL__iexact=trackerURL)
        if(issues.count() >= 1):
            raise BaseException('Already exists: '+trackerURL+". User %s"%user.id)


def _buildOfferFromDictionary_and_issue(dict, user, issue):
    offer = Offer.newOffer(issue, user, Decimal(0), '', '', False, False, None)
    _setOfferAttributesFromDictionary(offer, dict)
    return offer


def _setOfferAttributesFromDictionary(offer, dict):
    offer.price = Decimal(dict['price'])
    offer.currency = dict['currency']
    offer.no_forking = dict.has_key('no_forking')
    offer.require_release = dict.has_key('require_release')
    offer_check_expires = dict.has_key('expires')
    if(offer_check_expires):
        offer.set_expiration_days(int(dict['expiration_time']))
    offer.acceptanceCriteria = dict['acceptanceCriteria']

    if(not offer.acceptanceCriteria):
        raise BaseException('acceptanceCriteria is required')

    if(offer.price <= 0):
        raise BaseException('offer price must be a positive number')


def _throwIfAlreadySponsoring(issue, user):
    offer = get_or_none(Offer, issue=issue, sponsor=user, status__in=[Offer.OPEN, Offer.REVOKED])
    if(offer):
        raise BaseException('Already sponsoring: '+str(issue.id)+','+str(user.id))


def _throwIfNotIssueCreator(issue, user):
    if issue.createdByUser.id != user.id:
        raise BaseException('Security error. ' + str(user.id) + ' is not issue (' + str(issue.id) + ') creator. (Hey, if you do hack us, have the finesse to let us know, please - we are all firends here, right? ;-)')


def _throwIfNotOfferOwner(offer, user):
    if(not offer.sponsor.id == user.id):
        raise BaseException('Security error. '+str(user.id)+' is not offer ('+str(offer.id)+') owner. (Hey, if you do hack us, have the finesse to let us know, please - we are all firends here, right? ;-)')


def _throwIfSolutionInProgress(solution, user, operation=None):
    if(solution.status == Solution.IN_PROGRESS):
        raise BaseException('Error: found solution in progress '+str(solution.id)+', '+str(user.id)+'. operation: '+operation)


def _throwIfSolutionNotInProgress(solution, user, operation=None):
    if(not solution.status == Solution.IN_PROGRESS):
        raise BaseException('Error. Expected solution in IN_PROGRESS state. Found ('+solution.status+'). User '+str(user.id)+'/ solution '+str(solution.id)+'. operation: '+operation)


def _throwIfOfferNotOpen(offer, user, operation=None):
    if(not offer.status == Offer.OPEN):
        raise BaseException('Error. Expected offer in OPEN state. Found ('+offer.status+'). User '+str(user.id)+'/ offer '+str(offer.id)+'. operation: '+operation)


def _throwIfNotSolutionOwner(solution, user):
    if(not solution.programmer.id == user.id):
        raise BaseException('Security error. '+str(user.id)+' is not solution ('+str(solution.id)+') owner. (Hey, if you do hack us, have the finesse to let us know, please - we are all firends here, right? ;-)')


def _append_project_id_and_update_db_if_needed(issueInfo, trackerURL, user):
    issueInfo.project_id = ''
    project = None
    if issueInfo.project_trackerURL:
        found_projects = Project.objects.filter(trackerURL__iexact=issueInfo.project_trackerURL)
        if found_projects.count() > 1:
            notify_admin("WARNING: Database inconsistency", "more than one project found with url = %s"%issueInfo.project_trackerURL)
        elif found_projects.count() == 1:
            project = found_projects[0]
        else:
            project = _create_project(issueInfo, user)
    if project:
        if project.redirectto_project:
            project = project.redirectto_project
        issueInfo.project_id = project.id
        issueInfo.project_homeURL = project.homeURL


def _create_project(issueInfo, createdByUser):
    project = Project.newProject(issueInfo.project_name, createdByUser, '', issueInfo.project_trackerURL)
    project.save()
    notify_admin("INFO: Project created from json view", "issue key: "+issueInfo.key+"\n<br>"+ \
        "issue key: "+issueInfo.key+"\n<br>"+ \
        "project : "+project.name+"\n<br>"+ \
        "project.trackerURL: "+project.trackerURL+"\n<br>")
    return project

def validateIssueURL(url):
    parsedURL = urlparse(url)
    if parsedURL.scheme not in ('http', 'https'):
        return 'protocol must be http or https'
    elif not parsedURL.path or parsedURL.path == '/':
        return 'This is not a issue URL'
    else:
        return ''

def validateURL(url):
    parsedURL = urlparse(url)
    if parsedURL.scheme not in ('http', 'https'):
        return 'protocol must be http or https'
    elif not parsedURL.netloc or parsedURL.netloc.find('.') < 0:
        return 'invalid URL'
    else:
        return ''
